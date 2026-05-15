#!/usr/bin/env python3
"""Compute composite scores for stock-trend analysis.

Reads technical analysis output and optional dimension scores,
computes weighted composite score, trend direction, confidence,
and risk items.

Usage:
    python3 compute_scores.py --technical /tmp/technical.json [options]
    python3 compute_scores.py --technical /tmp/technical.json \
        --capital-flow-score 0.5 --fundamental-score 1 \
        --sentiment-score 1 --macro-score 0.5 --asset-type etf

Output JSON includes: scores, weights, composite_score, direction,
confidence, risks, special section, and all parameters for report generation.
"""

import argparse
import json
import math
import sys
from pathlib import Path


# --- Default weights ---

DEFAULT_WEIGHTS = {
    "technical": 0.35,
    "capital_flow": 0.25,
    "fundamental": 0.15,
    "sentiment": 0.15,
    "macro": 0.10,
}

FOCUS_WEIGHTS = {
    "technical": {
        "technical": 0.55, "capital_flow": 0.20,
        "fundamental": 0.083, "sentiment": 0.083, "macro": 0.084,
    },
    "capital_flow": {
        "capital_flow": 0.50, "technical": 0.20,
        "fundamental": 0.10, "sentiment": 0.10, "macro": 0.10,
    },
    "fundamental": {
        "fundamental": 0.45, "macro": 0.20,
        "technical": 0.117, "capital_flow": 0.117, "sentiment": 0.116,
    },
    "sentiment": {
        "sentiment": 0.45, "technical": 0.25,
        "capital_flow": 0.10, "fundamental": 0.10, "macro": 0.10,
    },
}

# Data quality weight adjustments
DATA_QUALITY_WEIGHTS = {
    "insufficient": {"technical": 0.175},  # 17.5% for tech, rest redistributed
    "limited": {"technical": 0.25},         # 25% for tech, rest redistributed
}

# --- Dimension validation rules ---

COVERAGE_RULES = {
    "capital_flow": {"min_items": 2, "penalty_factor": 0.5},
    "fundamental":  {"min_items": 2, "penalty_factor": 0.5},
    "sentiment":    {"min_items": 2, "penalty_factor": 0.5},
    "macro":        {"min_items": 2, "penalty_factor": 0.5},
}

EVENT_CAPS = {
    "macro":       {"single_event_max": 1.5, "high_score_min_signals": 2},
    "capital_flow":{"single_event_max": 1.0, "high_score_min_signals": 2},
    "fundamental": {"single_event_max": 1.0, "high_score_min_signals": 2},
    "sentiment":   {"single_event_max": 1.0, "high_score_min_signals": 2},
}


def redistribute_weights(base_weights, tech_weight_override):
    """Redistribute weights when technical weight changes due to data quality."""
    weights = dict(base_weights)
    old_tech = weights["technical"]
    new_tech = tech_weight_override
    # Scale other weights proportionally to fill the gap
    diff = old_tech - new_tech
    other_keys = [k for k in weights if k != "technical"]
    other_total = sum(weights[k] for k in other_keys)
    for k in other_keys:
        weights[k] = weights[k] + diff * (weights[k] / other_total) if other_total > 0 else weights[k]
    weights["technical"] = new_tech
    # Normalize to sum=1
    total = sum(weights.values())
    for k in weights:
        weights[k] = round(weights[k] / total, 4)
    return weights


def determine_direction(score):
    """Determine trend direction from composite score."""
    if score >= 2.0:
        return "看多"
    elif score <= -2.0:
        return "看空"
    else:
        return "震荡"


def determine_direction_modifier(score):
    """Determine direction modifier (偏多/偏空)."""
    if 0 < score < 2.0:
        return "偏多"
    elif -2.0 < score < 0:
        return "偏空"
    return ""


def determine_confidence(abs_score, consistency):
    """Determine confidence level based on score magnitude and consistency."""
    if abs_score >= 2.5 and consistency >= 0.7:
        return "高"
    elif abs_score >= 2.0 and consistency >= 0.5:
        return "中"
    else:
        return "低"


def apply_coverage_penalty(dim, covered_items, raw_score):
    """Apply score penalty when dimension has insufficient mandatory item coverage."""
    rule = COVERAGE_RULES.get(dim)
    if not rule:
        return raw_score, None
    if covered_items < rule["min_items"]:
        adjusted = raw_score * rule["penalty_factor"]
        warning = f"{dim}仅覆盖{covered_items}项，低于最低{rule['min_items']}项，得分×0.5"
        return round(adjusted, 2), warning
    return raw_score, None


def validate_event_cap(dim, score, signal_count):
    """Cap dimension score when backed by insufficient independent signals."""
    cap = EVENT_CAPS.get(dim)
    if not cap:
        return score, []
    adjusted = score
    warnings = []
    if signal_count < cap["high_score_min_signals"] and abs(score) > cap["single_event_max"]:
        direction = 1 if score > 0 else -1
        adjusted = round(direction * cap["single_event_max"], 2)
        warnings.append(
            f"{dim}得分{score}超出单事件封顶{cap['single_event_max']}且仅{signal_count}条信号，已修正为{adjusted}"
        )
    return adjusted, warnings


def validate_dimension_scores(scores, signals_info, self_check):
    """Validate all non-technical dimension scores for reasonableness.

    Args:
        scores: dict of {dim: score}
        signals_info: dict of {dim: {"count": int, "has_counter": bool}}
        self_check: dict of {dim: {"counter_found": bool, "adjusted": bool, "covered_items": int, ...}}

    Returns:
        adjusted_scores: dict of {dim: adjusted_score}
        warnings: list of warning strings
        confidence_penalty: float (0.0-1.0, multiplier for confidence)
    """
    adjusted = {}
    warnings = []
    penalty_factors = []

    for dim in ["capital_flow", "fundamental", "sentiment", "macro"]:
        score = scores.get(dim, 0)
        info = signals_info.get(dim, {})
        signal_count = info.get("count", 1)
        has_counter = info.get("has_counter", False)
        check = self_check.get(dim, {})

        # 1. Event cap (on raw score first)
        score, ws = validate_event_cap(dim, score, signal_count)
        warnings.extend(ws)

        # 2. Coverage penalty (on event-capped score)
        covered_items = check.get("covered_items", signal_count)
        score, w = apply_coverage_penalty(dim, covered_items, score)
        if w:
            warnings.append(w)

        # 3. Bullish/bearish balance: missing counter-signal reduces consistency
        if not has_counter:
            penalty_factors.append(0.6)
            warnings.append(f"{dim}缺少反向信号，一致性因子×0.6")

        # 4. Self-check adjustments
        if check.get("adjusted") and check.get("revised") is not None:
            score = round(check["revised"], 2)
            warnings.append(f"{dim}经逆向校验调整得分至{score}")

        adjusted[dim] = score

    # Technical dimension: no validation (script-computed)
    adjusted["technical"] = scores.get("technical", 0)

    # Overall confidence penalty
    overall_penalty = 1.0
    if penalty_factors:
        overall_penalty = min(penalty_factors)

    return adjusted, warnings, overall_penalty


def extract_risks(technical_data, score_data):
    """Extract risk items from technical analysis key_signals.

    Selects only risk-relevant signals and deduplicates by extracting
    distinct risk topics across compound signals.
    """
    risks = []

    summary = technical_data.get("summary", {})
    key_signals = summary.get("key_signals", [])

    # Risk keywords for signal classification
    risk_keywords = ["背离", "死叉", "极度收口", "超买", "压力", "空头", "下跌", "减仓", "止损",
                     "净流出", "净流出"]

    # Track extracted risk topics to avoid redundancy
    # e.g. "RSI顶背离" and "RSI=46.3...顶背离" both talk about RSI divergence
    extracted_topics = set()

    for signal in key_signals:
        is_risk = any(kw in signal for kw in risk_keywords)
        if not is_risk:
            continue

        # Determine the primary risk topic from this signal
        topic = None
        for kw in ["极度收口", "顶背离", "底背离", "死叉", "背离", "超买", "超卖", "净流出"]:
            if kw in signal:
                # Extract indicator name before the keyword
                idx = signal.find(kw)
                # Find which indicator this signal is about
                for ind in ["RSI", "KDJ", "MACD", "布林带", "OBV", "ADX", "均线", "量价"]:
                    if ind in signal:
                        topic = f"{ind}:{kw}"
                        break
                if not topic:
                    topic = kw
                break

        # Normalize: "RSI=46.3，中性区间；顶背离" → topic "RSI:背离"
        # "RSI顶背离" → topic "RSI:背离"
        # "中轨下方运行；布林带极度收口(3%分位，即将变盘)" → topic "布林带:极度收口"

        if topic and topic not in extracted_topics:
            risks.append(signal)
            extracted_topics.add(topic)
        elif not topic:
            # Signals without a clear topic (e.g. generic warnings)
            risks.append(signal)

    # Add risk_reward_warning if present
    rr_warning = summary.get("risk_reward_warning")
    if rr_warning:
        risks.append(rr_warning)

    return risks


def build_special_section(asset_type, technical_data, etf_data=None, capital_flow_data=None):
    """Build special section for ETF/HK/ST assets."""
    if asset_type == "etf" and etf_data:
        nav = etf_data.get("nav", {})
        content_parts = []
        iopv_premium = nav.get("iopv_premium_pct")
        if iopv_premium is not None:
            direction = "溢价" if iopv_premium > 0 else "折价"
            content_parts.append(f"IOPV折溢价率: {iopv_premium:+.2f}%（{direction}）")
        nav_val = nav.get("nav")
        if nav_val:
            content_parts.append(f"最新净值: {nav_val}")
        fund_name = etf_data.get("fund_name", "")
        if fund_name:
            content_parts.insert(0, f"基金名称: {fund_name}")

        return {
            "type": "etf",
            "title": "ETF 特殊分析",
            "content": "；".join(content_parts) if content_parts else "",
        }

    elif asset_type == "hk":
        return {
            "type": "hk",
            "title": "港股特殊分析",
            "content": "需补充恒指联动、卖空占比、南向资金、AH溢价数据",
        }

    elif asset_type == "st":
        return {
            "type": "st",
            "title": "退市风险警示",
            "content": "ST/*ST标的，基本面强制-1分，注意退市风险",
        }

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Compute composite scores for stock-trend analysis"
    )
    parser.add_argument(
        "--technical", required=True,
        help="Path to technical analysis JSON"
    )
    parser.add_argument("--capital-flow-score", type=float, default=None,
                        help="Capital flow dimension score (-3 to +3)")
    parser.add_argument("--fundamental-score", type=float, default=None,
                        help="Fundamental dimension score (-3 to +3)")
    parser.add_argument("--sentiment-score", type=float, default=None,
                        help="Sentiment dimension score (-3 to +3)")
    parser.add_argument("--macro-score", type=float, default=None,
                        help="Macro dimension score (-3 to +3)")
    parser.add_argument("--focus", choices=["technical", "capital_flow", "fundamental", "sentiment"],
                        help="Focus dimension for weight adjustment")
    parser.add_argument("--asset-type", choices=["etf", "hk", "st", "stock"],
                        default="stock", help="Asset type for special sections")
    parser.add_argument("--etf-data", help="Path to ETF data JSON")
    parser.add_argument("--capital-flow-data", help="Path to capital flow JSON")
    parser.add_argument("--fundamental-data", help="Path to fundamental data JSON (automated scoring)")
    parser.add_argument("--macro-data", help="Path to macro snapshot JSON (automated scoring)")
    # Dimension summaries (rich text from agent's research, passed to report)
    parser.add_argument("--capital-summary", default="",
                        help="Capital flow dimension summary text")
    parser.add_argument("--fundamental-summary", default="",
                        help="Fundamental dimension summary text")
    parser.add_argument("--sentiment-summary", default="",
                        help="Sentiment dimension summary text")
    parser.add_argument("--macro-summary", default="",
                        help="Macro dimension summary text")
    # Comprehensive analysis for 综合研判 section
    parser.add_argument("--analysis", default=None,
                        help="JSON object with core_conflict, events (array of {date,event,impact}), advice (array of strings)")
    parser.add_argument("--risks", help="JSON array of additional risk strings")
    parser.add_argument("--self-check", default=None,
                        help="JSON with self-check results per dimension: {dim: {counter_found, adjusted, covered_items, original, revised}}")
    parser.add_argument("--signals-info", default=None,
                        help="JSON with signal counts per dimension: {dim: {count, has_counter}}")
    parser.add_argument("-o", "--output", default="/tmp/scores.json",
                        help="Output JSON file path")
    args = parser.parse_args()

    # Read technical analysis
    with open(args.technical, "r", encoding="utf-8") as f:
        technical_data = json.load(f)

    summary = technical_data.get("summary", {})

    # Extract technical score from summary
    tech_score = summary.get("total_score", 0)
    data_quality = summary.get("data_quality")

    # Non-technical scores: use provided values or derive from automated data
    # Agent-provided scores always take precedence
    scores = {
        "technical": round(tech_score, 2),
        "capital_flow": round(args.capital_flow_score, 2) if args.capital_flow_score is not None else 0,
        "fundamental": round(args.fundamental_score, 2) if args.fundamental_score is not None else 0,
        "sentiment": round(args.sentiment_score, 2) if args.sentiment_score is not None else 0,
        "macro": round(args.macro_score, 2) if args.macro_score is not None else 0,
    }

    automated_sources = {}

    # Automated fundamental scoring (only when agent doesn't provide explicit score)
    if args.fundamental_data and args.fundamental_score is None:
        try:
            with open(args.fundamental_data, "r", encoding="utf-8") as f:
                fund_data = json.load(f)
            fund_summary = fund_data.get("summary", {})
            fund_score = 0
            dq = fund_summary.get("data_quality")
            if dq in ("good", "partial"):
                pe_pct = fund_summary.get("pe_percentile_3y")
                rev_growth = fund_summary.get("revenue_growth_pct")
                profit_growth = fund_summary.get("profit_growth_pct")
                roe = fund_summary.get("roe")
                if pe_pct is not None:
                    if pe_pct < 30:
                        fund_score += 1
                    elif pe_pct > 70:
                        fund_score -= 1
                if rev_growth is not None and profit_growth is not None:
                    if rev_growth > 10 and profit_growth > 10:
                        fund_score += 1
                    elif rev_growth < -5 and profit_growth < -5:
                        fund_score -= 1
                if roe is not None and roe > 15:
                    fund_score += 1
                fund_score = max(-3, min(3, fund_score))
            scores["fundamental"] = fund_score
            automated_sources["fundamental"] = dq
        except Exception:
            pass

    # Automated macro scoring (only when agent doesn't provide explicit score)
    if args.macro_data and args.macro_score is None:
        try:
            with open(args.macro_data, "r", encoding="utf-8") as f:
                macro_data = json.load(f)
            ms = macro_data.get("summary", {})
            macro_score = 0
            dq = ms.get("data_quality")
            if dq in ("good", "partial"):
                hs300 = ms.get("hs300", {})
                if isinstance(hs300, dict):
                    hs300_chg = hs300.get("change_pct")
                    if hs300_chg is not None:
                        if hs300_chg > 1:
                            macro_score += 1
                        elif hs300_chg < -1:
                            macro_score -= 1
                usd_cny = ms.get("usd_cny", {})
                if isinstance(usd_cny, dict):
                    cny_chg = usd_cny.get("change_pct")
                    if cny_chg is not None and cny_chg < 0:
                        macro_score += 1  # CNY strengthening
                pmi = ms.get("pmi")
                if pmi is not None:
                    if pmi >= 50:
                        macro_score += 1
                    elif pmi < 48:
                        macro_score -= 1
                macro_score = max(-3, min(3, macro_score))
            scores["macro"] = macro_score
            automated_sources["macro"] = dq
        except Exception:
            pass

    # Automated capital flow scoring (when agent doesn't provide explicit score)
    # The enhanced capital flow script now provides northbound/margin data
    if args.capital_flow_data and args.capital_flow_score is None:
        try:
            with open(args.capital_flow_data, "r", encoding="utf-8") as f:
                cap_data = json.load(f)
            ext = cap_data.get("data_extended", {})
            cap_score = 0
            nb = ext.get("northbound_individual", {})
            if nb and isinstance(nb, dict):
                chg = nb.get("change_shares")
                if chg is not None:
                    if chg > 0:
                        cap_score += 1
                    elif chg < 0:
                        cap_score -= 1
            margin = ext.get("margin", {})
            if margin and isinstance(margin, dict):
                mb = margin.get("margin_balance_billion")
                if mb is not None and mb > 100:
                    cap_score += 1
            cap_score = max(-3, min(3, cap_score))
            if cap_score != 0:
                scores["capital_flow"] = scores.get("capital_flow", 0) + cap_score
                scores["capital_flow"] = max(-3, min(3, scores["capital_flow"]))
                automated_sources["capital_flow"] = "enhanced"
        except Exception:
            pass

    # Parse self-check and signals-info
    self_check = {}
    if args.self_check:
        try:
            self_check = json.loads(args.self_check)
        except json.JSONDecodeError:
            pass
    signals_info = {}
    if args.signals_info:
        try:
            signals_info = json.loads(args.signals_info)
        except json.JSONDecodeError:
            pass

    # Validate dimension scores
    validation_warnings = []
    confidence_penalty = 1.0
    if self_check or signals_info:
        scores, validation_warnings, confidence_penalty = validate_dimension_scores(
            scores, signals_info, self_check
        )

    # Compute weights
    if args.focus:
        weights = dict(FOCUS_WEIGHTS[args.focus])
    else:
        weights = dict(DEFAULT_WEIGHTS)

    # Adjust for data quality
    if data_quality in DATA_QUALITY_WEIGHTS:
        tech_override = DATA_QUALITY_WEIGHTS[data_quality]["technical"]
        weights = redistribute_weights(weights, tech_override)

    # Compute composite score
    composite = sum(scores[k] * weights[k] for k in scores)
    composite = round(composite, 3)

    # Determine direction and confidence
    direction = determine_direction(composite)
    direction_modifier = determine_direction_modifier(composite)
    consistency = summary.get("consistency", 0)
    # Apply validation penalty to consistency before confidence determination
    adjusted_consistency = consistency * confidence_penalty if confidence_penalty < 1.0 else consistency
    confidence = determine_confidence(abs(composite), adjusted_consistency)

    # If self-check resulted in score adjustments, downgrade confidence one level
    any_adjusted = any(v.get("adjusted") for v in self_check.values()) if self_check else False
    if any_adjusted:
        confidence_map = {"高": "中", "中": "低", "低": "低"}
        confidence = confidence_map.get(confidence, confidence)

    # Full direction string
    if direction_modifier:
        full_direction = f"{direction}{direction_modifier}"
    else:
        full_direction = direction

    # Extract risks
    risks = extract_risks(technical_data, scores)
    if args.risks:
        try:
            extra_risks = json.loads(args.risks)
            risks.extend(extra_risks)
        except json.JSONDecodeError:
            pass

    # Deduplicate risks while preserving order (by exact string AND topic)
    seen_text = set()
    seen_topics = set()
    unique_risks = []
    for r in risks:
        if r in seen_text:
            continue
        # Extract risk topic for semantic dedup
        topic = None
        for kw in ["极度收口", "顶背离", "底背离", "死叉", "背离", "超买", "超卖", "净流出", "净流入"]:
            if kw in r:
                for ind in ["RSI", "KDJ", "MACD", "布林带", "OBV", "ADX", "均线", "量价", "资金"]:
                    if ind in r:
                        topic = f"{ind}:{kw}"
                        break
                if not topic:
                    topic = kw
                break
        if topic and topic in seen_topics:
            continue
        seen_text.add(r)
        if topic:
            seen_topics.add(topic)
        unique_risks.append(r)

    # Build special section
    etf_data = None
    if args.etf_data and Path(args.etf_data).exists():
        with open(args.etf_data, "r", encoding="utf-8") as f:
            etf_data = json.load(f)

    special = build_special_section(
        args.asset_type, technical_data,
        etf_data=etf_data,
    )

    # Build output
    output = {
        "scores": scores,
        "weights": {k: round(v, 4) for k, v in weights.items()},
        "composite_score": composite,
        "direction": full_direction,
        "direction_detail": direction,
        "direction_modifier": direction_modifier,
        "confidence": confidence,
        "consistency": consistency,
        "risks": unique_risks,
        "special": special,
        "data_quality": data_quality,
        # Automated data sources used for scoring
        "automated_sources": automated_sources,
        # Validation results
        "validation_warnings": validation_warnings,
        "confidence_penalty": confidence_penalty,
        # Dimension summaries (rich text from agent research, passed through to report)
        "dimension_summaries": {
            "capital": args.capital_summary,
            "fundamental": args.fundamental_summary,
            "sentiment": args.sentiment_summary,
            "macro": args.macro_summary,
        },
        # Comprehensive analysis for 综合研判 section in report
        "analysis": json.loads(args.analysis) if args.analysis else None,
        # Convenience fields for report generation
        "report_params": {
            "direction": full_direction,
            "score": composite,
            "confidence": confidence,
            "ts_code": technical_data.get("meta", {}).get("ts_code", ""),
            "stop_loss": summary.get("stop_loss"),
            "target_conservative": summary.get("target_conservative"),
            "target_moderate": summary.get("target_moderate"),
            "target_aggressive": summary.get("target_aggressive"),
            "risk_reward_ratio": summary.get("risk_reward_ratio"),
            "favorable_rr": summary.get("favorable_rr"),
            "position_sizing": summary.get("position_sizing"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "support_levels": summary.get("support_levels", []),
            "resistance_levels": summary.get("resistance_levels", []),
        },
    }

    # Write output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Print summary
    print(f"Composite score: {composite} | Direction: {full_direction} | Confidence: {confidence}")
    print(f"Scores: tech={scores['technical']} cap={scores['capital_flow']} "
          f"fund={scores['fundamental']} sent={scores['sentiment']} macro={scores['macro']}")
    print(f"Weights: " + " ".join(f"{k}={v:.2f}" for k, v in weights.items()))
    print(f"Risks: {unique_risks}")
    print(f"Output: {output_path}")
    if validation_warnings:
        print(f"Validation warnings: {validation_warnings}")
    if confidence_penalty < 1.0:
        print(f"Confidence penalty: {confidence_penalty}")


if __name__ == "__main__":
    main()