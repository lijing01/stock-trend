"""Pure, fail-closed LPS trade-plan assessment.

The adapter is intentionally separate from ``candidate_trade_plan/v1``.  It
uses one frozen next-session open opportunity and never falls back to weekday
arithmetic when an exchange calendar is absent.
"""

import copy
import math
from datetime import date
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

from analysis.lps_distance_research import classify_lps_record
from core.candidate_trade_plan import MIN_PRIMARY_RR, RISK_BUDGET_PCT


ITEM_SCHEMA_VERSION = "lps-trade-assessment-item/v1"
PLAN_SCHEMA_VERSION = "lps-trade-plan/v1"
RUN_SCHEMA_VERSION = "lps-trade-assessment-run/v1"
DEFINITION_FROZEN_ON = "2026-10-01"
ENTRY_ATR_WIDTH = 0.5
MIN_STOP_BUFFER_ATR = 0.30
MIN_STOP_BUFFER_PCT = 0.50
FORMAL_BUCKETS = {"actionable", "waiting_trigger"}

STATUS_READY = "可制定交易计划"
STATUS_WAIT = "等待触发"
STATUS_UNAVAILABLE = "不可计划"
STATUS_INSUFFICIENT = "数据不足"


def _number(value, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if positive and value <= 0:
        return None
    if nonnegative and value < 0:
        return None
    return value


def _day(value):
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _next_session(basis_date, sessions):
    basis = _day(basis_date)
    if basis is None or not isinstance(sessions, list):
        return None, "calendar_missing"
    normalized = [_day(value) for value in sessions]
    if any(value is None for value in normalized):
        return None, "calendar_invalid"
    if normalized != sorted(set(normalized)):
        return None, "calendar_not_sorted_unique"
    if basis not in normalized:
        return None, "calendar_basis_missing"
    following = [value for value in normalized if value > basis]
    return (following[0], None) if following else (None, "calendar_coverage_insufficient")


def _formal_allocation(policy, context):
    # Only the frozen production policy grants allocation. External evidence
    # may further cap that allocation, never enlarge it.
    value = _number((policy or {}).get("max_portfolio_pct"), nonnegative=True)
    cap = _number((context.get("formal_market_allocation") or {}).get("allocation_pct"), nonnegative=True)
    return min(value, cap) if value is not None and cap is not None else value


def _security_eligibility(code, basis_date, meta):
    reasons = []
    if not isinstance(meta, dict) or not meta:
        return {"eligible": False, "reasons": ["security_metadata_missing"]}
    meta_code = str(meta.get("code") or "")
    if meta_code != str(code):
        reasons.append("security_code_mismatch")
    as_of = _day(meta.get("as_of"))
    if not meta.get("source"):
        reasons.append("security_source_missing")
    if as_of is None or as_of > basis_date:
        reasons.append("security_metadata_point_in_time_invalid")
    exchange = str(meta.get("exchange") or "").upper()
    board = str(meta.get("board") or "").lower().replace("-", "_")
    kind = str(meta.get("security_type") or "").lower().replace("-", "_")
    if exchange not in {"SH", "SSE", "SZ", "SZSE"}:
        reasons.append("exchange_not_supported")
    if board not in {"main", "main_board", "mainboard", "sh_main", "sz_main"}:
        reasons.append("board_not_supported")
    if kind not in {"ordinary_a_share", "common_stock", "a_share", "ordinary_stock"}:
        reasons.append("security_type_not_supported")
    if "is_st" not in meta:
        reasons.append("st_status_missing")
    elif meta.get("is_st") is not False:
        reasons.append("st_ineligible")
    if "ipo_special_rules" not in meta:
        reasons.append("ipo_rule_status_missing")
    elif meta.get("ipo_special_rules") is not False:
        reasons.append("ipo_special_rules_ineligible")
    lot_size = _number(meta.get("lot_size"), positive=True)
    if lot_size is None:
        reasons.append("lot_size_missing")
    elif lot_size != 100:
        reasons.append("lot_size_not_supported")
    if _number(meta.get("price_limit_pct"), positive=True) != 10:
        reasons.append("ordinary_main_board_rule_missing")
    if not (len(str(code)) == 6 and str(code).isdigit()
            and str(code).startswith(("600", "601", "603", "605", "000", "001", "002", "003"))):
        reasons.append("ordinary_a_share_code_invalid")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "exchange": exchange or None,
        "board": board or None,
        "security_type": kind or None,
        "is_st": meta.get("is_st"),
        "ipo_special_rules": meta.get("ipo_special_rules"),
        "lot_size": meta.get("lot_size"),
        "as_of": as_of,
        "source": meta.get("source"),
    }


def _availability_valid(known_at, decision_at):
    try:
        known = datetime.fromisoformat(str(known_at).replace("Z", "+00:00"))
        decision = datetime.fromisoformat(str(decision_at).replace("Z", "+00:00"))
        zone = ZoneInfo("Asia/Shanghai")
        return (known if known.tzinfo else known.replace(tzinfo=zone)) <= (decision if decision.tzinfo else decision.replace(tzinfo=zone))
    except (TypeError, ValueError):
        return False


def _resistance_prices(target_evidence, basis_date):
    if not isinstance(target_evidence, dict):
        return [], ["target_evidence_missing"]
    evidence_day = _day(target_evidence.get("basis_date") or target_evidence.get("as_of"))
    source = target_evidence.get("source")
    if evidence_day is None or evidence_day > basis_date or not source:
        return [], ["target_evidence_point_in_time_invalid"]
    raw = (target_evidence.get("resistances") or
           target_evidence.get("resistance_levels") or
           target_evidence.get("resistance") or [])
    if not isinstance(raw, list):
        raw = [raw]
    values = []
    for item in raw:
        value = item.get("price") if isinstance(item, dict) else item
        number = _number(value, positive=True)
        if number is not None:
            values.append(number)
    return sorted(set(values)), ([] if values else ["resistance_target_missing"])


def _trusted_announcement_url(value):
    try:
        parsed = urlparse(str(value or ""))
        host = parsed.hostname or ""
    except ValueError:
        return False
    return parsed.scheme == "https" and any(
        host == domain or host.endswith("." + domain)
        for domain in ("cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn"))


def _event_check(context, decision_at=None):
    raw = context.get("event_check")
    if not isinstance(raw, dict):
        return {"status": "pending", "display_status": "待核验",
                "reason": "event_check_missing"}
    status = str(raw.get("status") or "pending").lower()
    proofs = raw.get("evidence") or []
    trusted = bool(proofs) and all(
        isinstance(proof, dict)
        and _trusted_announcement_url(proof.get("url"))
        and _availability_valid(proof.get("published_at"), decision_at)
        and _availability_valid(proof.get("known_at"), decision_at)
        for proof in proofs)
    if not trusted or not _availability_valid(raw.get("reviewed_at"), decision_at):
        status = "pending"
    if status in {"clear", "verified_clear"}:
        display = "已核验"
    elif status in {"risk", "blocked", "material_risk"}:
        display = "存在风险"
        status = "risk"
    else:
        display = "待核验"
        status = "pending"
    return {"status": status, "display_status": display,
            "reason": raw.get("reason"), "source": raw.get("source"),
            "reviewed_at": raw.get("reviewed_at"), "trusted_evidence": trusted,
            "evidence": copy.deepcopy(proofs)}


def _counterargument(record, context):
    candidate = record.get("candidate") or {}
    value = (context.get("counterargument") or candidate.get("counterargument") or
             record.get("counterargument"))
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "LPS 可能是假突破后的弱反弹；若量能、板块或公告证据恶化，计划失效。"


def _empty_item(record, basis_date, evidence, status, reasons, event_check,
                security=None, allocation_pct=None, next_session=None):
    candidate = record.get("candidate") or {}
    return {
        "schema_version": ITEM_SCHEMA_VERSION,
        "definition_frozen_on": DEFINITION_FROZEN_ON,
        "record_id": record.get("record_id"),
        "research_snapshot_sha256": record.get("research_snapshot_sha256"),
        "code": str(record.get("code") or candidate.get("code") or ""),
        "name": candidate.get("name") or record.get("name"),
        "basis_date": basis_date,
        "formal_bucket": record.get("final_status") or candidate.get("formal_bucket"),
        "status": status,
        "status_reasons": sorted(set(reasons)),
        "lps_evidence": evidence,
        "security_eligibility": security or {"eligible": False, "reasons": []},
        "market_eligibility": {"eligible": False, "allocation_pct": allocation_pct},
        "next_entry_session": next_session,
        "event_check": event_check,
        "plan": None,
    }


def build_lps_trade_assessment(record, policy, basis_date, context):
    """Build one auditable LPS plan item from point-in-time frozen inputs.

    ``context`` must be selected by the caller using the compound identity
    ``record_id@research_snapshot_sha256``.  This function never searches by
    code and never fills historical evidence from current data.
    """
    record = copy.deepcopy(record or {})
    context = copy.deepcopy(context or {})
    policy = copy.deepcopy(policy or {})
    basis_date = _day(basis_date)
    event_check = _event_check(context, record.get("decision_at"))
    evidence = classify_lps_record(record, basis_date) if basis_date else {
        "eligible": False, "reasons": ["basis_date_invalid"]}
    formal_bucket = record.get("final_status") or (record.get("candidate") or {}).get("formal_bucket")
    code = str(record.get("code") or (record.get("candidate") or {}).get("code") or "")
    next_session, calendar_error = _next_session(basis_date, context.get("market_sessions"))
    security = _security_eligibility(code, basis_date, context.get("security_meta")) \
        if basis_date else {"eligible": False, "reasons": ["basis_date_invalid"]}
    allocation = _formal_allocation(policy, context)
    missing = []
    if not context:
        missing.append("assessment_context_missing")
    if not _availability_valid(context.get("known_at"), record.get("decision_at")):
        missing.append("decision_context_availability_unknown_or_future")
    calendar = context.get("calendar_evidence") or {}
    if (not calendar.get("source") or not calendar.get("calendar_id")
            or not _day(calendar.get("complete_through"))
            or (next_session and _day(calendar.get("complete_through")) < next_session)):
        missing.append("calendar_evidence_incomplete")
    if context.get("price_scale") not in {"raw", "adjusted"}:
        missing.append("price_scale_missing_or_invalid")
    scale_proof = context.get("price_scale_evidence") or {}
    if (not scale_proof.get("source") or not scale_proof.get("scale_id")
            or scale_proof.get("price_scale") != context.get("price_scale")
            or not _availability_valid(scale_proof.get("known_at"), record.get("decision_at"))):
        missing.append("signal_price_scale_evidence_missing")
    if (context.get("target_evidence") or {}).get("price_scale") != context.get("price_scale"):
        missing.append("target_price_scale_mismatch")
    if calendar_error:
        missing.append(calendar_error)
    missing.extend(reason for reason in evidence.get("reasons", []) if reason in {
        "final_close_evidence_missing", "price_or_atr_invalid", "structural_floor_missing",
        "signal_age_invalid", "state_not_confirmed_holding",
    })
    missing.extend(reason for reason in security.get("reasons", []) if reason in {
        "security_metadata_missing", "security_metadata_point_in_time_invalid",
        "st_status_missing", "ipo_rule_status_missing", "lot_size_missing",
        "security_source_missing", "ordinary_main_board_rule_missing",
    })
    if allocation is None:
        missing.append("formal_market_allocation_missing")
    if basis_date is None:
        missing.append("basis_date_invalid")
    if missing:
        return _empty_item(record, basis_date, evidence, STATUS_INSUFFICIENT,
                           missing, event_check, security, allocation, next_session)

    blockers = []
    if not evidence.get("is_lps"):
        blockers.append("not_lps")
    if not evidence.get("eligible"):
        blockers.extend(evidence.get("reasons") or ["lps_evidence_ineligible"])
    if formal_bucket not in FORMAL_BUCKETS:
        blockers.append("formal_bucket_ineligible")
    if str(policy.get("mode") or "") not in FORMAL_BUCKETS:
        blockers.append("market_policy_ineligible")
    if not security.get("eligible"):
        blockers.extend(security.get("reasons") or ["security_ineligible"])
    if allocation <= 0:
        blockers.append("formal_market_allocation_zero")
    if event_check["status"] == "risk":
        blockers.append("event_risk_blocked")

    trigger = _number(evidence.get("trigger_close"), positive=True)
    atr = _number(evidence.get("current_atr"), positive=True)
    stop = _number(evidence.get("structural_floor"), positive=True)
    if trigger is None or atr is None or stop is None:
        blockers.append("plan_level_missing")
    entry_low = trigger
    entry_high = trigger + ENTRY_ATR_WIDTH * atr if trigger and atr else None
    required_buffer = (max(MIN_STOP_BUFFER_ATR * atr,
                           entry_low * MIN_STOP_BUFFER_PCT / 100)
                       if entry_low and atr else None)
    actual_buffer = entry_low - stop if entry_low and stop else None
    if (required_buffer is None or actual_buffer is None or
            actual_buffer + 1e-9 < required_buffer):
        blockers.append("stop_buffer_insufficient")

    resistances, target_errors = _resistance_prices(
        context.get("target_evidence"), basis_date)
    if target_errors:
        missing.extend(target_errors)
    target = next((value for value in resistances
                   if entry_high and stop and entry_high > stop and value > entry_high
                   and (value - entry_high) / (entry_high - stop) >= MIN_PRIMARY_RR), None)
    if resistances and target is None:
        blockers.append("resistance_rr_below_minimum")
    if missing:
        return _empty_item(record, basis_date, evidence, STATUS_INSUFFICIENT,
                           missing, event_check, security, allocation, next_session)
    if blockers:
        return _empty_item(record, basis_date, evidence, STATUS_UNAVAILABLE,
                           blockers, event_check, security, allocation, next_session)

    rr_low = (target - entry_low) / (entry_low - stop)
    rr_high = (target - entry_high) / (entry_high - stop)
    max_position = min(20.0, allocation,
                       RISK_BUDGET_PCT / ((entry_high - stop) / entry_high * 100) * 100)
    price_scale = str(context.get("price_scale") or "").lower()
    if price_scale not in {"raw", "adjusted"}:
        return _empty_item(record, basis_date, evidence, STATUS_INSUFFICIENT,
                           ["price_scale_missing_or_invalid"], event_check,
                           security, allocation, next_session)
    costs = copy.deepcopy(context.get("cost_config"))
    cost_known = (isinstance(costs, dict) and bool(costs.get("contract_id"))
                  and all(_number(costs.get(field), nonnegative=True) is not None
                          for field in ("commission_bps", "minimum_commission_cny",
                                        "sell_stamp_tax_bps", "slippage_bps_each_side"))
                  and costs.get("commission_includes_exchange_and_transfer_fees") is True
                  and bool(costs.get("tax_source"))
                  and _day(costs.get("tax_effective_from")) is not None
                  and _day(costs.get("tax_verified_through")) is not None
                  and _day(costs.get("tax_effective_from")) <= basis_date <= _day(costs.get("tax_verified_through")))
    current_close = _number(evidence.get("current_close"), positive=True)
    ready_now = (formal_bucket == "actionable" and current_close is not None
                 and entry_low <= current_close <= entry_high)
    status = STATUS_READY if ready_now else STATUS_WAIT
    immutable_identity = (
        f"{record.get('record_id')}@{record.get('research_snapshot_sha256')}"
        if record.get("record_id") and record.get("research_snapshot_sha256") else None
    )
    if immutable_identity is None:
        return _empty_item(record, basis_date, evidence, STATUS_INSUFFICIENT,
                           ["immutable_identity_missing"], event_check,
                           security, allocation, next_session)
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "definition_frozen_on": DEFINITION_FROZEN_ON,
        "record_id": record.get("record_id"),
        "research_snapshot_sha256": record.get("research_snapshot_sha256"),
        "immutable_identity": immutable_identity,
        "plan_status": "ready",
        "code": code,
        "basis_date": basis_date,
        "entry_method": "next_session_open_only_v1",
        "entry": {"low": round(entry_low, 4), "high": round(entry_high, 4),
                  "trigger_price": round(trigger, 4), "atr": round(atr, 4)},
        "stop_loss": {"price": round(stop, 4), "source": "frozen_structural_floor",
                      "trigger_mode": "intraday_touch"},
        "stop_buffer": {"required": round(required_buffer, 4),
                        "actual": round(actual_buffer, 4), "valid": True},
        "targets": {"primary": round(target, 4), "source": "frozen_resistance",
                    "available_resistances": [round(value, 4) for value in resistances]},
        "risk_reward": {"minimum": MIN_PRIMARY_RR,
                        "rr_at_entry_low": round(rr_low, 2),
                        "rr_at_entry_high": round(rr_high, 2)},
        "position": {"risk_budget_pct": RISK_BUDGET_PCT,
                     "max_portfolio_pct": round(max_position, 2)},
        "market_eligibility": {"eligible": True, "allocation_pct": allocation},
        "validity": {"trading_sessions": 1, "entry_session": next_session},
        "price_scale": price_scale,
        "price_scale_id": scale_proof.get("scale_id"),
        "event_check": event_check,
        "decision_context_evidence": {"known_at": context.get("known_at"),
                                      "calendar": calendar,
                                      "security_meta": context.get("security_meta"),
                                      "target_evidence": context.get("target_evidence")},
        "cost_config": costs if cost_known else {"status": "cost_unknown"},
        "net_return_eligible": cost_known,
        "counterargument": _counterargument(record, context),
        "invalidation": "开盘不在入场区间、开盘低于或等于结构失效位，或事件风险升级",
    }
    return {
        "schema_version": ITEM_SCHEMA_VERSION,
        "definition_frozen_on": DEFINITION_FROZEN_ON,
        "record_id": record.get("record_id"),
        "research_snapshot_sha256": record.get("research_snapshot_sha256"),
        "code": code,
        "name": (record.get("candidate") or {}).get("name") or record.get("name"),
        "basis_date": basis_date,
        "formal_bucket": formal_bucket,
        "status": status,
        "status_reasons": (["formal_waiting_trigger"] if formal_bucket == "waiting_trigger"
                           else ["outside_entry_zone"] if not ready_now else []),
        "lps_evidence": evidence,
        "security_eligibility": security,
        "market_eligibility": plan["market_eligibility"],
        "next_entry_session": next_session,
        "event_check": event_check,
        "plan": plan,
    }


def validate_lps_trade_plan(plan, expected_date=None):
    reasons = []
    if not isinstance(plan, dict):
        return {"complete": False, "reasons": ["plan_missing"]}
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        reasons.append("plan_schema_invalid")
    if expected_date and plan.get("basis_date") != _day(expected_date):
        reasons.append("plan_basis_date_mismatch")
    entry = plan.get("entry") or {}
    low = _number(entry.get("low"), positive=True)
    high = _number(entry.get("high"), positive=True)
    stop = _number((plan.get("stop_loss") or {}).get("price"), positive=True)
    target = _number((plan.get("targets") or {}).get("primary"), positive=True)
    if not low or not high or low > high:
        reasons.append("entry_invalid")
    if not stop or not low or stop >= low:
        reasons.append("stop_invalid")
    rr = ((target - high) / (high - stop)
          if target and high and stop and target > high > stop else None)
    if rr is None or rr + 1e-9 < MIN_PRIMARY_RR:
        reasons.append("risk_reward_below_minimum")
    validity = plan.get("validity") or {}
    if validity.get("trading_sessions") != 1 or not _day(validity.get("entry_session")):
        reasons.append("one_session_validity_invalid")
    if plan.get("entry_method") != "next_session_open_only_v1":
        reasons.append("entry_method_invalid")
    if not (plan.get("market_eligibility") or {}).get("eligible"):
        reasons.append("market_ineligible")
    if plan.get("price_scale") not in {"raw", "adjusted"}:
        reasons.append("price_scale_invalid")
    if not str(plan.get("counterargument") or "").strip():
        reasons.append("counterargument_missing")
    return {"complete": not reasons, "reasons": reasons,
            "recomputed_rr_at_entry_high": round(rr, 2) if rr is not None else None}


def build_lps_trade_assessment_run(records, policy, basis_date, contexts,
                                   research_snapshot_sha256, simulations=None):
    """Build a report artifact using compound-identity-only input joins."""
    items = []
    contexts = contexts if isinstance(contexts, dict) else {}
    simulations = simulations if isinstance(simulations, dict) else {}
    for source in records or []:
        record = copy.deepcopy(source) if isinstance(source, dict) else {}
        record["research_snapshot_sha256"] = research_snapshot_sha256
        identity = (f"{record.get('record_id')}@{research_snapshot_sha256}"
                    if record.get("record_id") and research_snapshot_sha256 else None)
        item = build_lps_trade_assessment(
            record, policy, basis_date, contexts.get(identity) if identity else None)
        simulation = simulations.get(identity) if identity else None
        simulation_identity = (simulation or {}).get("immutable_identity") \
            if isinstance(simulation, dict) else None
        if isinstance(simulation, dict) and simulation_identity == identity:
            item["simulation"] = copy.deepcopy(simulation)
        elif simulation is not None:
            item.setdefault("status_reasons", []).append("simulation_identity_mismatch")
        items.append(item)
    counts = {status: sum(item.get("status") == status for item in items)
              for status in (STATUS_READY, STATUS_WAIT, STATUS_UNAVAILABLE,
                             STATUS_INSUFFICIENT)}
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "definition_frozen_on": DEFINITION_FROZEN_ON,
        "basis_date": _day(basis_date),
        "research_snapshot_sha256": research_snapshot_sha256,
        "status": "completed" if all(item.get("plan") for item in items) \
            else "completed_with_gaps",
        "record_count": len(items),
        "status_counts": counts,
        "items": items,
    }
