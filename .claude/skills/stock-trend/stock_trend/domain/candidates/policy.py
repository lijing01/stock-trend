"""Recommendation policy and bucket allocation with no IO dependencies."""

from .ranking import candidate_quality_score, candidate_rank_score


def short_term_observation_reason(item):
    signal_status = item.get("wyckoff", {}).get("short_term", {}).get("signal_status")
    return {"retest_pending": "wyckoff_retest_pending", "failed_breakout": "wyckoff_failed_breakout"}.get(signal_status)


def candidate_gate_pass(item, min_score, policy=None):
    if candidate_quality_score(item) < min_score:
        return False
    if not item.get("data_quality", {}).get("eligible", False):
        return False
    if not item.get("sector_actionable", True):
        return False
    if policy and policy.get("requires_sector_capital_proof") and item.get("sector_capital_evidence") != "positive_verified":
        return False
    return short_term_observation_reason(item) is None


def build_recommendation_policy(regime, expected_date, market_open=False):
    if not regime or regime.get("score") is None:
        policy = {"mode": "observation", "max_recommendations": 0, "reasons": ["regime_missing"]}
    elif regime.get("data_date") != expected_date:
        policy = {"mode": "observation", "max_recommendations": 0, "reasons": ["regime_stale"]}
    elif regime.get("data_quality") in ("missing", "unknown") and "data_quality" in regime:
        policy = {"mode": "observation", "max_recommendations": 0,
                  "reasons": ["regime_data_missing" if regime.get("data_quality") == "missing" else "regime_data_quality_unknown"],
                  "missing_components": list(regime.get("missing_components") or [])}
    elif regime.get("data_quality") == "partial":
        policy = {"mode": "observation", "max_recommendations": 0, "reasons": ["regime_data_partial"],
                  "missing_components": list(regime.get("missing_components") or []),
                  "partial_components": list(regime.get("partial_components") or [])}
    else:
        score = float(regime["score"])
        divergence = regime.get("capital_score") is not None and float(regime["capital_score"]) < 35
        if score < 60:
            policy = {"mode": "observation", "max_recommendations": 0, "reasons": ["regime_weak"]}
        elif score < 80:
            policy = {"mode": "waiting_trigger", "max_recommendations": 2, "reasons": [], "requires_sector_capital_proof": divergence}
        else:
            policy = {"mode": "actionable", "max_recommendations": 5, "reasons": [], "requires_sector_capital_proof": divergence}
    if market_open:
        policy.update({"provisional": True, "provisional_target_mode": policy.get("mode", "observation")})
        policy["reasons"] = (policy.get("reasons") or []) + ["intraday_provisional"]
    return policy


def select_candidate_pool(scored, top, min_score, policy=None,
                          priority_bonuses=None, priority_applier=None):
    candidates = [item for item in scored if item.get("composite_score", 0) >= min_score]
    for item in candidates:
        if priority_applier is not None:
            priority_applier(item)
        else:
            level = item.get("buy_point_level")
            key = (str(level) if str(level).startswith("strict_level_")
                   else f"strict_level_{level}") if level is not None else ""
            bonus = float((priority_bonuses or {}).get(
                key, item.get("buy_point_priority_bonus", 0.0)) or 0.0)
            item["buy_point_priority_bonus"] = bonus
            item["execution_priority_score"] = round(
                min(100.0, candidate_quality_score(item) + bonus), 1)
        item["score_eligible"] = candidate_quality_score(item) >= min_score
    candidates.sort(key=lambda item: (candidate_gate_pass(item, min_score, policy), candidate_rank_score(item), str(item.get("code", ""))), reverse=True)
    return candidates[:top]


def classify_candidates(candidates, policy):
    data_rejected, eligible_candidates = [], []
    for item in candidates:
        quality = item.get("data_quality", {})
        if quality.get("eligible", False):
            eligible_candidates.append(item)
            continue
        rejected = dict(item)
        reasons = list(quality.get("reasons", [])) or ["data_quality_ineligible"]
        if not item.get("sector_actionable", True):
            reasons.append(item.get("sector_persistence_status") or item.get("sector_type") or "sector_unverified")
        reasons.extend(policy.get("reasons", []))
        reason = short_term_observation_reason(item)
        if reason:
            reasons.append(reason)
        rejected["observation_reasons"] = list(dict.fromkeys(reasons))
        data_rejected.append(rejected)
    eligible = [item for item in eligible_candidates if item.get("sector_actionable", True) and item.get("score_eligible", True)
                and (not policy.get("requires_sector_capital_proof", False) or item.get("sector_capital_evidence") == "positive_verified")
                and short_term_observation_reason(item) is None]
    limit = policy.get("max_recommendations", 0)
    actionable = eligible[:limit] if policy.get("mode") == "actionable" else []
    waiting = eligible[:limit] if policy.get("mode") == "waiting_trigger" else []
    promoted = {item["code"] for item in actionable + waiting}
    confirmations = []
    if policy.get("mode") == "waiting_trigger":
        confirmations = [item for item in eligible_candidates if item.get("data_quality", {}).get("eligible", False)
                         and item.get("score_eligible", True) and item.get("wyckoff")
                         and short_term_observation_reason(item) is None and item.get("code") not in promoted][:2]
        confirmations = [dict(item, confirmation_conditions="次日板块跑赢沪深300、守住当日低点，且放量或资金/共振确认") for item in confirmations]
    confirmation_codes = {item["code"] for item in confirmations}
    observation = []
    for item in eligible_candidates:
        if item.get("code") in promoted or item.get("code") in confirmation_codes:
            continue
        copied = dict(item)
        reasons = list(item.get("data_quality", {}).get("reasons", []))
        if not item.get("sector_actionable", True):
            reasons.append(item.get("sector_persistence_status") or item.get("sector_type") or "sector_unverified")
        if policy.get("requires_sector_capital_proof", False) and item.get("sector_capital_evidence") != "positive_verified":
            reasons.append("breadth_capital_divergence")
        if not item.get("score_eligible", True):
            reasons.append("quality_adjusted_below_min_score")
        reason = short_term_observation_reason(item)
        if reason:
            reasons.append(reason)
        if not reasons and policy.get("reasons"):
            reasons.extend(policy["reasons"])
        if not reasons:
            reasons.append("recommendation_limit")
        if policy.get("provisional") and "intraday_provisional" not in reasons:
            reasons.insert(0, "intraday_provisional")
        copied["observation_reasons"] = list(dict.fromkeys(reasons))
        observation.append(copied)
    return {"actionable": actionable, "waiting_trigger": waiting, "next_day_confirmation": confirmations,
            "observation": observation, "data_rejected": data_rejected}
