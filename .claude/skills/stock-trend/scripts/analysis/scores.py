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

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from core.cache_utils import CACHE_DIR, load_iopv_history, save_iopv_history
from core.eastmoney_utils import piecewise_linear


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

# IOPV premium → capital_flow score contribution ([-1.5, +1.5] range)
# Slight discount is optimal (buyers get good price), deep discount is modest positive
# (may signal liquidity concerns), premium is negative (overpaying).
IOPV_CAPITAL_FLOW_ANCHORS = [
    (-2.0, 0.5),    # Deep discount — modest positive, but liquidity risk
    (-1.0, 0.8),    # Moderate discount — positive
    (-0.5, 1.2),    # Decent discount — good
    (-0.3, 1.5),    # Slight discount — best entry signal
    (-0.1, 1.0),    # Near-zero discount — positive
    (0.0, 0.0),     # Fair value — neutral
    (0.15, -0.5),   # Small premium — slight negative
    (0.3, -1.0),    # Moderate premium — negative
    (1.0, -1.5),    # Large premium — strongly negative
    (2.0, -1.5),    # Extreme premium — strongly negative (capped)
]


def score_iopv_capital_flow(iopv_premium_pct):
    """Convert IOPV premium/discount % to a capital_flow score contribution.

    Returns a value in [-1.5, +1.5] range, suitable for adding to the
    capital_flow dimension score (which is on the [-3, +3] scale).
    """
    return round(piecewise_linear(float(iopv_premium_pct), IOPV_CAPITAL_FLOW_ANCHORS), 2)


def score_shares_trend_from_etf_data(etf_data: dict) -> float | None:
    """Compute shares trend score from etf_data (same logic as etf_scanner.py).

    Returns 0-100 score or None if insufficient data.
    """
    if not etf_data:
        return None
    recent_flows = etf_data.get("recent_flows")
    if not isinstance(recent_flows, list) or len(recent_flows) < 2:
        return None
    valid = [r for r in recent_flows if isinstance(r, dict) and r.get("shares_billion") is not None]
    if len(valid) < 2:
        return None
    first = float(valid[0]["shares_billion"])
    last = float(valid[-1]["shares_billion"])
    if first == 0:
        return None
    change_pct = (last - first) / abs(first) * 100
    SHARES_TREND_ANCHORS = [
        (-20, 0.0), (-10, 10.0), (-3, 20.0), (0, 40.0),
        (3, 65.0), (10, 85.0), (30, 100.0),
    ]
    return round(piecewise_linear(change_pct, SHARES_TREND_ANCHORS), 1)


def _compute_lhb_sentiment(lhb_data: dict) -> float:
    """Compute sentiment adjustment from 龙虎榜 data.

    Returns adjustment in [-1.0, +0.8] range on sentiment score scale.

    Adjustments:
        机构净买入 ≥ 2家                    → +0.8
        机构净卖出 ≥ 2家                    → -1.0
        纯游资主导, 无机构                   → -0.3
        散户主导买入                         → -1.0
        游资净买入 + 机构净卖出              → -0.5 (分歧)
        上榜但机构交易额 < 20%               → -0.3
    """
    adjustment = 0.0

    if not lhb_data.get("is_on_board"):
        return 0.0

    has_inst_buy = lhb_data.get("has_institution_buy", False)
    has_inst_sell = lhb_data.get("has_institution_sell", False)
    retail = lhb_data.get("retail_dominated", False)
    youzi = lhb_data.get("has_floating_capital", False)
    risk_level = lhb_data.get("risk_level", "low")

    if retail:
        adjustment -= 1.0

    if has_inst_sell and not has_inst_buy:
        adjustment -= 1.0

    if has_inst_buy and not has_inst_sell:
        adjustment += 0.8

    if has_inst_buy and has_inst_sell:
        adjustment -= 0.5
    if has_inst_buy and youzi:
        adjustment -= 0.3

    if youzi and not has_inst_buy and not has_inst_sell:
        adjustment -= 0.3

    adjustment = max(-1.0, min(0.8, adjustment))
    return round(adjustment, 2)


def score_iopv_capital_flow_enhanced(
    iopv_premium_pct: float, history: list | None = None, shares_trend_dir: int = 0
) -> dict:
    """Enhanced IOPV scoring with historical percentile + trend + shares linkage.

    Args:
        iopv_premium_pct: Current IOPV premium % (+=premium, -=discount).
        history: List of {date, premium} dicts, newest last. None = no history.
        shares_trend_dir: +1 shares growing, -1 shrinking, 0 flat/unknown.

    Returns dict with components and combined score in [-1.5, +1.5].
    """
    base_score = score_iopv_capital_flow(iopv_premium_pct)

    # Component 1: absolute premium/discount (existing anchor mapping)
    abs_component = base_score * 0.5  # weight 50%

    # Component 2: historical percentile (if history available)
    percentile_component = 0.0
    percentile_info = None
    if history and len(history) >= 5:
        premiums = sorted([float(e["premium"]) for e in history])
        n = len(premiums)
        rank = sum(1 for p in premiums if p < iopv_premium_pct)
        pct_rank = rank / n  # 0.0 = cheapest, 1.0 = most expensive
        # Score: cheap percentile (0.0-0.3) → positive, expensive (0.7-1.0) → negative
        if pct_rank < 0.3:
            percentile_component = 0.6 * (1 - pct_rank / 0.3)  # 0 → 0.6
        elif pct_rank > 0.7:
            percentile_component = -0.6 * (pct_rank - 0.7) / 0.3  # 0 → -0.6
        percentile_info = round(pct_rank * 100, 0)

    # Component 3: 3-day premium trend
    trend_component = 0.0
    trend_info = None
    if history and len(history) >= 3:
        recent = history[-3:]
        if all(isinstance(e, dict) and "premium" in e for e in recent):
            p0 = float(recent[0]["premium"])
            p2 = float(recent[-1]["premium"])
            # Narrowing premium (negative delta = improving) → positive
            delta = p2 - p0  # positive = premium widening (bad)
            if abs(delta) > 0.05:  # > 0.05% change is meaningful
                trend_component = -delta * 0.3  # capped implicitly
                trend_component = max(-0.4, min(0.4, trend_component))
                trend_info = "收窄" if delta < 0 else "扩大"

    # Component 4: premium × shares linkage
    shares_component = 0.0
    shares_info = None
    if abs(iopv_premium_pct) > 0.3:  # meaningful premium/discount
        if iopv_premium_pct > 0.3 and shares_trend_dir == 1:
            # Premium + shares growing → fund recognized, mild positive adjustment
            shares_component = 0.3
            shares_info = "溢价+份额增长=资金认可"
        elif iopv_premium_pct > 0.3 and shares_trend_dir == -1:
            # Premium + shares shrinking → speculative, extra negative
            shares_component = -0.5
            shares_info = "溢价+份额缩水=纯炒作风险"
        elif iopv_premium_pct < -0.3 and shares_trend_dir == 1:
            # Discount + shares growing → accumulation signal
            shares_component = 0.2
            shares_info = "折价+份额增长=资金吸筹"

    combined = abs_component + percentile_component + trend_component + shares_component
    combined = max(-1.5, min(1.5, combined))

    return {
        "score": round(combined, 2),
        "components": {
            "absolute": base_score,
            "percentile": round(percentile_component, 2),
            "trend": round(trend_component, 2),
            "shares_linkage": round(shares_component, 2),
        },
        "percentile_rank": percentile_info,
        "trend_direction": trend_info,
        "shares_signal": shares_info,
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


def determine_confidence(abs_score, consistency, atr_pct=None):
    """Determine confidence level based on score magnitude and consistency.

    Thresholds adapt to volatility:
        low-vol (ATR<2%):  high≥2.0&0.6, mid≥1.5&0.4
        normal  (ATR2-4%): high≥2.5&0.7, mid≥2.0&0.5  (default)
        high-vol(ATR>4%):  high≥3.0&0.8, mid≥2.5&0.6
    """
    if atr_pct is not None:
        if atr_pct < 2.0:
            high_t, high_c, mid_t, mid_c = 2.0, 0.6, 1.5, 0.4
        elif atr_pct > 4.0:
            high_t, high_c, mid_t, mid_c = 3.0, 0.8, 2.5, 0.6
        else:
            high_t, high_c, mid_t, mid_c = 2.5, 0.7, 2.0, 0.5
    else:
        high_t, high_c, mid_t, mid_c = 2.5, 0.7, 2.0, 0.5

    if abs_score >= high_t and consistency >= high_c:
        return "高"
    elif abs_score >= mid_t and consistency >= mid_c:
        return "中"
    else:
        return "低"


def calc_inter_dim_consistency(scores):
    """Compute cross-dimension directional consistency.

    Measures agreement across all 5 dimensions (technical, capital_flow,
    fundamental, sentiment, macro). Neutral dims (score=0) excluded from
    count — they neither agree nor disagree.

    Returns:
        float: 0.0 (all against) ~ 1.0 (all agree), 1.0 if all neutral.
    """
    directions = []
    for dim in ["technical", "capital_flow", "fundamental", "sentiment", "macro"]:
        s = scores.get(dim, 0)
        if s > 0.3:
            directions.append(1)
        elif s < -0.3:
            directions.append(-1)

    if not directions:
        return 1.0  # all neutral = no disagreement

    majority = max(directions.count(1), directions.count(-1))
    return round(majority / len(directions), 2)


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
    if score is None:
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

        # 3. Data availability: skip counter-signal penalty if no real data exists
        #    (capital_flow=0 records, covered_items < min, or data_quality=skip)
        has_data = covered_items >= 2 or signal_count >= 2
        if not has_data:
            warnings.append(f"{dim}数据不足(条目={signal_count}, 已检={covered_items})，跳过信心惩罚")
        else:
            # Fall back to self_check counter_found (from skill agent's manual research)
            if not has_counter:
                has_counter = check.get("counter_found", False)

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
                     "净流出"]

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


def build_special_section(asset_type, technical_data, etf_data=None, capital_flow_data=None, futures_data=None):
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

        # Futures data for ETF
        if futures_data and futures_data.get("meta", {}).get("data_source") not in ("error", "unavailable", "unsupported", None):
            basis = futures_data.get("basis", {})
            signals = futures_data.get("signals", {})
            if basis and basis.get("basis_pct") is not None:
                content_parts.append(f"期现价差: {basis['basis_pct']:+.2f}%（{basis.get('direction', '')}）")
            if signals and signals.get("composite_signal"):
                content_parts.append(f"期货信号: {signals['composite_signal']}")

        return {
            "type": "etf",
            "title": "ETF 特殊分析",
            "content": "；".join(content_parts) if content_parts else "",
        }

    elif asset_type == "hk":
        return None

    elif asset_type == "st":
        return {
            "type": "st",
            "title": "退市风险警示",
            "content": "ST/*ST标的，基本面强制-1分，注意退市风险",
        }

    return None


def validate_input(technical_data, dimension_scores, data_dir=None):
    """Validate input data for score computation.

    Args:
        technical_data: dict of technical analysis output
        dimension_scores: dict of {dim: score} dimension scores
        data_dir: optional Path or str pointing to data directory

    Returns:
        List of error strings (empty list = all checks pass)
    """
    errors = []

    # Check a: technical_data must be a dict
    if not isinstance(technical_data, dict):
        errors.append(
            f"technical_data must be a dict, got {type(technical_data).__name__}"
        )
        return errors  # no point checking deeper

    # Check b: technical_data["summary"] must be a dict with required keys
    summary = technical_data.get("summary")
    if not isinstance(summary, dict):
        errors.append(
            f"technical_data['summary'] must be a dict, got {type(summary).__name__}"
        )
    else:
        required_summary_keys = {
            "total_score": (int, float),
            "direction": str,
            "confidence": (int, float),
        }
        for key, expected_types in required_summary_keys.items():
            val = summary.get(key)
            if val is None:
                errors.append(
                    f"technical_data['summary'] missing required key '{key}'"
                )
            elif not isinstance(val, expected_types):
                errors.append(
                    f"technical_data['summary']['{key}'] expected "
                    f"{expected_types[0].__name__ if isinstance(expected_types, tuple) else expected_types.__name__}, "
                    f"got {type(val).__name__}={val!r}"
                )

    # Check c: technical_data["data_quality"] must be a valid enum value
    valid_qualities = ("good", "limited", "insufficient", "partial")
    dq = technical_data.get("data_quality")
    if dq is not None and dq not in valid_qualities:
        errors.append(
            f"technical_data['data_quality'] must be one of {valid_qualities}, "
            f"got {dq!r}"
        )

    # Check d: all dimension_scores values must be numeric and in [-100, 100]
    if not isinstance(dimension_scores, dict):
        errors.append(
            f"dimension_scores must be a dict, got {type(dimension_scores).__name__}"
        )
    else:
        for dim, score in dimension_scores.items():
            if not isinstance(score, (int, float)):
                errors.append(
                    f"dimension_scores['{dim}'] must be numeric, got {type(score).__name__}={score!r}"
                )
            elif score < -3 or score > 3:
                errors.append(
                    f"dimension_scores['{dim}'] out of range [-3, +3]: {score}"
                )

    # Check e: if data_dir is provided and exists, check dimension data files
    if data_dir is not None:
        data_path = Path(data_dir) if not isinstance(data_dir, Path) else data_dir
        if data_path.exists():
            required_dim_files = ["capital_flow.json", "fundamental.json", "macro_snapshot.json"]
            for filename in required_dim_files:
                fpath = data_path / filename
                if not fpath.exists():
                    errors.append(
                        f"dimension data file missing: {fpath}"
                    )

    return errors


def find_data_file(data_dir, filename):
    """Find a data file in the data directory, return (parsed_json, path) or (None, path)."""
    path = Path(data_dir) / filename
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f), str(path)
        except (json.JSONDecodeError, OSError):
            pass
    return None, str(path)


def main():
    parser = argparse.ArgumentParser(
        description="Compute composite scores for stock-trend analysis"
    )
    parser.add_argument(
        "--technical",
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
    parser.add_argument("--futures-data", help="Path to futures data JSON (ETF only)")
    parser.add_argument("--index-valuation", help="Path to index valuation JSON (ETF only)")
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
                        help="JSON object with core_conflict, events (array of {date,event,impact} or {name,detail,impact}), advice (array of strings)")
    parser.add_argument("--risks", help="JSON array of additional risk strings")
    parser.add_argument("--self-check", default=None,
                        help="JSON with self-check results per dimension: {dim: {counter_found, adjusted, covered_items, original, revised}}")
    parser.add_argument("--signals-info", default=None,
                        help="JSON with signal counts per dimension: {dim: {count, has_counter}}")
    parser.add_argument("--code", help="Stock/ETF code to locate data directory")
    parser.add_argument("--data-dir", help="Data directory path (default: .cache/stock-trend/{code}/)")
    parser.add_argument("-o", "--output", default="/tmp/scores.json",
                        help="Output JSON file path")
    args = parser.parse_args()

    # Resolve data directory from --code
    data_dir = None
    if args.code:
        data_dir = Path(args.data_dir) if args.data_dir else Path(CACHE_DIR) / args.code
        if not data_dir.exists():
            print(f"Error: data directory not found: {data_dir}", file=sys.stderr)
            sys.exit(1)

        # Auto-infer asset_type from resolve.json if not explicitly set
        if args.asset_type == "stock":  # default value, user didn't override
            resolve_path = data_dir / "resolve.json"
            if resolve_path.exists():
                try:
                    with open(resolve_path, "r", encoding="utf-8") as _f:
                        _resolve = json.load(_f)
                    _asset = _resolve.get("asset", "")
                    if _asset == "FD":
                        args.asset_type = "etf"
                except Exception:
                    pass

    # Read technical analysis
    if data_dir:
        technical_data, tech_path = find_data_file(data_dir, "technical.json")
        if technical_data is None:
            print(f"Error: technical.json not found in {data_dir}", file=sys.stderr)
            sys.exit(1)
    elif args.technical:
        with open(args.technical, "r", encoding="utf-8") as f:
            technical_data = json.load(f)
    else:
        parser.error("--technical or --code required")

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

    # Validate input before score computation
    validation_errors = validate_input(technical_data, scores, data_dir)
    if validation_errors:
        print("⚠ Input validation warnings:", file=sys.stderr)
        for err in validation_errors:
            print(f"  - {err}", file=sys.stderr)

    # Populate dimension data args from data directory when --code is used
    if data_dir:
        if not args.fundamental_data:
            _, args.fundamental_data = find_data_file(data_dir, "fundamental.json")
        if not args.macro_data:
            _, args.macro_data = find_data_file(data_dir, "macro_snapshot.json")
        if not args.capital_flow_data:
            _, args.capital_flow_data = find_data_file(data_dir, "capital_flow.json")
        if not args.etf_data:
            _, args.etf_data = find_data_file(data_dir, "etf_data.json")
        if not args.index_valuation:
            _, args.index_valuation = find_data_file(data_dir, "index_valuation.json")

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

    # Automated ETF index valuation scoring (contributes to fundamental for ETFs)
    # Uses index PE percentile same logic as stock fundamental
    if args.index_valuation and args.fundamental_score is None and args.asset_type == "etf":
        try:
            with open(args.index_valuation, "r", encoding="utf-8") as f:
                iv_data = json.load(f)
            iv_summary = iv_data.get("summary", {})
            iv_dq = iv_summary.get("data_quality")
            if iv_dq in ("good", "partial"):
                iv_score = 0
                pe_pct_3y = iv_summary.get("pe_percentile_3y")
                pe_pct_20d = iv_summary.get("pe_percentile_20d")
                div_yield = iv_summary.get("dividend_yield_pct")

                # Use 3-yr percentile if available (legulegu), else 20-day (csindex)
                if pe_pct_3y is not None:
                    if pe_pct_3y < 30:
                        iv_score += 1
                    elif pe_pct_3y > 70:
                        iv_score -= 1
                elif pe_pct_20d is not None:
                    if pe_pct_20d < 20:
                        iv_score += 1
                    elif pe_pct_20d > 80:
                        iv_score -= 1

                # Dividend yield bonus for ETFs
                if div_yield is not None and div_yield > 3:
                    iv_score += 1
                elif div_yield is not None and div_yield < 0.5:
                    iv_score -= 1

                iv_score = max(-3, min(3, iv_score))
                # Merge into existing fundamental score (starts at 0 for ETFs)
                scores["fundamental"] = round(scores.get("fundamental", 0) + iv_score, 2)
                scores["fundamental"] = max(-3, min(3, scores["fundamental"]))
                automated_sources["fundamental_index_pe"] = iv_summary.get("pe_ttm")
                automated_sources["fundamental_data_source"] = iv_data.get("meta", {}).get("data_source")
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

    # P3 #10: Automated sentiment scoring from northbound flow + margin data
    if args.capital_flow_data and args.sentiment_score is None:
        try:
            with open(args.capital_flow_data, "r", encoding="utf-8") as f:
                cap_data = json.load(f)
            ext = cap_data.get("data_extended", {})
            sent_score = 0.0
            auto_src = []

            # Market-level northbound: consecutive inflow days
            nb_market = ext.get("northbound_market")
            if isinstance(nb_market, list) and len(nb_market) >= 3:
                streak = 0
                for day in reversed(nb_market):
                    if (day.get("net_buy_billion") or 0) > 0:
                        streak += 1
                    else:
                        break
                if streak >= 3:
                    sent_score += 1.0
                elif streak >= 1:
                    sent_score += 0.3
                latest_net = nb_market[-1].get("net_buy_billion", 0) or 0
                if latest_net < -8:
                    sent_score -= 1.0
                elif latest_net < -3:
                    sent_score -= 0.5
                auto_src.append("northbound")

            # Individual stock northbound holding change
            nb_ind = ext.get("northbound_individual")
            if isinstance(nb_ind, dict):
                chg = nb_ind.get("change_shares")
                if chg is not None:
                    sent_score += 0.5 if chg > 0 else (-0.5 if chg < -10 else 0)
                    auto_src.append("nb_ind")

            # Margin balance - high = market confidence
            margin = ext.get("margin")
            if isinstance(margin, dict):
                mb = margin.get("margin_balance_billion")
                if mb is not None:
                    if mb > 200:
                        sent_score += 0.5
                    elif mb < 30:
                        sent_score -= 0.5
                    auto_src.append("margin")

            sent_score = max(-3, min(3, sent_score))
            if sent_score != 0 or auto_src:
                scores["sentiment"] = round(scores["sentiment"] + sent_score, 2)
                scores["sentiment"] = max(-3, min(3, scores["sentiment"]))
                if auto_src:
                    automated_sources["sentiment"] = "+".join(auto_src)
        except Exception:
            pass

    # ── 龙虎榜 sentiment adjustment ──────────────────────────────────
    if data_dir and args.sentiment_score is None:
        lhb_data, _ = find_data_file(data_dir, "longhubang.json")
        if lhb_data and isinstance(lhb_data, dict):
            lhb_adjustment = _compute_lhb_sentiment(lhb_data)
            if lhb_adjustment != 0:
                scores["sentiment"] = round(scores["sentiment"] + lhb_adjustment, 2)
                scores["sentiment"] = max(-3, min(3, scores["sentiment"]))
                automated_sources["sentiment_longhubang"] = lhb_adjustment

    # Automated futures scoring (ETF only, when futures data is available)
    if args.futures_data and args.asset_type == "etf":
        try:
            fut_path = args.futures_data
            if not Path(fut_path).exists() and data_dir:
                fut_path = str(Path(data_dir) / "futures_data.json")
            if Path(fut_path).exists():
                with open(fut_path, "r", encoding="utf-8") as f:
                    fut_data = json.load(f)
                fut_meta = fut_data.get("meta", {})
                fut_signals = fut_data.get("signals", {})
                if fut_meta.get("data_source") not in ("error", "unavailable", "unsupported", None):
                    basis_score = fut_signals.get("basis_score", 0)
                    oi_score = fut_signals.get("oi_score", 0)
                    volume_score = fut_signals.get("volume_score", 0)
                    # Basis + OI contribute to capital_flow
                    if basis_score != 0 or oi_score != 0:
                        futures_capital = round((basis_score + oi_score) / 2, 2)
                        scores["capital_flow"] = round(scores.get("capital_flow", 0) + futures_capital, 2)
                        scores["capital_flow"] = max(-3, min(3, scores["capital_flow"]))
                        automated_sources["capital_flow_futures"] = fut_meta.get("data_source", "eastmoney")
                    # Volume confirmation contributes to sentiment
                    if volume_score != 0:
                        scores["sentiment"] = round(scores.get("sentiment", 0) + volume_score * 0.5, 2)
                        scores["sentiment"] = max(-3, min(3, scores["sentiment"]))
                        automated_sources["sentiment_futures"] = fut_meta.get("data_source", "eastmoney")
        except Exception:
            pass

    # Load ETF and futures data for IOPV scoring and special section
    etf_data = None
    etf_source = None
    futures_data = None
    if data_dir:
        etf_data, etf_source = find_data_file(data_dir, "etf_data.json")
        futures_data, _ = find_data_file(data_dir, "futures_data.json")
    elif args.etf_data and Path(args.etf_data).exists():
        with open(args.etf_data, "r", encoding="utf-8") as f:
            etf_data = json.load(f)
    if args.futures_data and Path(args.futures_data).exists() and futures_data is None:
        with open(args.futures_data, "r", encoding="utf-8") as f:
            futures_data = json.load(f)

    # ── P3 #11: Fund size change trend + share change rate → fundamental (ETF) ──
    if args.asset_type == "etf" and args.fundamental_score is None and etf_data and isinstance(etf_data, dict):
        try:
            fund_size_add = 0.0
            recent_flows = etf_data.get("recent_flows")
            if isinstance(recent_flows, list) and len(recent_flows) >= 3:
                mid = len(recent_flows) // 2
                first_half_avg = sum(
                    float(r["shares_billion"]) for r in recent_flows[:mid]
                    if r.get("shares_billion") is not None
                ) / max(sum(1 for r in recent_flows[:mid] if r.get("shares_billion") is not None), 1)
                second_half_avg = sum(
                    float(r["shares_billion"]) for r in recent_flows[mid:]
                    if r.get("shares_billion") is not None
                ) / max(sum(1 for r in recent_flows[mid:] if r.get("shares_billion") is not None), 1)
                if first_half_avg > 0:
                    trend_pct = (second_half_avg - first_half_avg) / abs(first_half_avg) * 100
                    if trend_pct > 5:
                        fund_size_add += 0.5
                    elif trend_pct > 2:
                        fund_size_add += 0.2
                    elif trend_pct < -5:
                        fund_size_add -= 0.5
                    elif trend_pct < -2:
                        fund_size_add -= 0.2
            # Fund size growth = capital认可
            fund_size = etf_data.get("fund_size", {})
            shares = fund_size.get("shares_billion") if isinstance(fund_size, dict) else None
            if shares is not None and shares > 50:
                fund_size_add += 0.5
            if fund_size_add != 0:
                scores["fundamental"] = round(scores["fundamental"] + fund_size_add, 2)
                scores["fundamental"] = max(-3, min(3, scores["fundamental"]))
                automated_sources["fundamental"] = (
                    automated_sources.get("fundamental", "") + "_fundsize"
                    if automated_sources.get("fundamental")
                    else "fundsize"
                )
        except Exception:
            pass

    # Automated IOPV premium scoring for ETFs — P2 #9: enhanced with history + trend + shares linkage
    iopv_enhanced_info = None
    if args.asset_type == "etf" and args.capital_flow_score is None:
        iopv_premium = None
        if etf_data and isinstance(etf_data, dict):
            nav = etf_data.get("nav")
            if isinstance(nav, dict):
                iopv_premium = nav.get("iopv_premium_pct")
        if iopv_premium is not None:
            try:
                # Determine shares_trend direction from etf_data
                shares_dir = 0
                st = score_shares_trend_from_etf_data(etf_data)
                if st is not None:
                    if st >= 60:
                        shares_dir = 1
                    elif st <= 40:
                        shares_dir = -1

                # Load history and compute enhanced score
                iopv_history = load_iopv_history()
                code_history = iopv_history.get(args.code, []) if args.code else []
                enhanced = score_iopv_capital_flow_enhanced(
                    float(iopv_premium), history=code_history, shares_trend_dir=shares_dir
                )
                iopv_score = enhanced["score"]
                if iopv_score != 0:
                    scores["capital_flow"] = round(scores.get("capital_flow", 0) + iopv_score, 2)
                    scores["capital_flow"] = max(-3, min(3, scores["capital_flow"]))
                automated_sources["capital_flow_iopv"] = round(float(iopv_premium), 4)
                automated_sources["iopv_enhanced"] = enhanced["components"]
                iopv_enhanced_info = enhanced

                # Persist to history cache
                if args.code:
                    save_iopv_history(iopv_history, args.code, float(iopv_premium))
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

    # Extract ATR for volatility-adaptive confidence thresholds
    atr_pct = technical_data.get("latest", {}).get("atr", {}).get("atr_pct")
    if atr_pct is None:
        atr_pct = summary.get("atr_pct")
    consistency = summary.get("consistency", 0)
    # Blend with inter-dimension directional consistency
    # Technical consistency measures indicator agreement within price action.
    # Inter-dim consistency measures cross-factor directional agreement.
    # Weight: 40% technical (indicator-level), 60% cross-dim (strategic-level)
    inter_consistency = calc_inter_dim_consistency(scores)
    blended_consistency = round(0.4 * consistency + 0.6 * inter_consistency, 2)
    # Apply validation penalty to blended consistency before confidence determination
    adjusted_consistency = blended_consistency * confidence_penalty if confidence_penalty < 1.0 else blended_consistency
    confidence = determine_confidence(abs(composite), adjusted_consistency, atr_pct=atr_pct)

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

    # Build special section (etf_data and futures_data loaded earlier for IOPV scoring)
    special = build_special_section(
        args.asset_type, technical_data,
        etf_data=etf_data,
        futures_data=futures_data,
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
            "rr_conservative": summary.get("rr_conservative"),
            "rr_moderate": summary.get("rr_moderate"),
            "rr_aggressive": summary.get("rr_aggressive"),
            "favorable_rr": summary.get("favorable_rr"),
            "position_sizing": summary.get("position_sizing"),
            "position_tier": summary.get("position_tier"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "support_levels": summary.get("support_levels", []),
            "resistance_levels": summary.get("resistance_levels", []),
            "entry_verdict": summary.get("entry_signals", {}).get("verdict", "wait"),
            "entry_signals": summary.get("entry_signals", {}).get("signals", []),
            "entry_signal_count": summary.get("entry_signals", {}).get("signal_count", 0),
        },
    }

    # Write output
    if data_dir:
        output_path = data_dir / "scores.json"
    else:
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