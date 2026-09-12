"""Auditable score ledger shared by candidate scoring and report rendering."""
import copy
from datetime import datetime, timezone


SCHEMA_VERSION = "candidate-score-ledger/v1"
QUANT_RULE_VERSION = "stock-scanner/composite-v1"
WYCKOFF_RULE_VERSION = "daily-candidates/strict-buy-point-v1"
NEWS_RULE_VERSION = "candidate-news-overlay/v1"


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _date_or_none(value):
    """Snapshot date fields must be ISO calendar days, never placeholders."""
    if value is None:
        return None
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text if len(text) == 10 and text[4] == "-" and text[7] == "-" else None


def _source(item, dimension):
    evidence = (item.get("source_evidence") or {}).get(dimension, {})
    quality = ((item.get("data_quality") or {}).get("dimensions") or {}).get(dimension, {})
    return {
        "data_provider": evidence.get("source") or quality.get("source") or "scanner",
        "data_date": _date_or_none(evidence.get("data_date") or quality.get("data_date")),
        "fetched_at": evidence.get("fetched_at") or evidence.get("fetch_time") or "unknown",
        "quality": quality.get("quality") or evidence.get("status") or "unknown",
    }


def build_quantitative_ledger(item):
    """Build the immutable quantitative portion before later overlays.

    Dimension scores are weighted components of the raw composite; they are
    deliberately not represented as independent bonuses.
    """
    dimensions = item.get("raw_dimensions") or item.get("dimensions") or {}
    has_wyckoff = "wyckoff" in dimensions
    weights = ({"momentum": .25, "volume_price": .15, "capital": .15,
                "fundamental": .10, "sector_strength": .10, "wyckoff": .25}
               if has_wyckoff else
               {"momentum": .30, "volume_price": .20, "capital": .20,
                "fundamental": .15, "sector_strength": .15})
    entries = []
    for name, weight in weights.items():
        score = _number(dimensions.get(name))
        source_name = "kline" if name in {"momentum", "volume_price", "wyckoff"} else (
            "capital" if name == "capital" else "fundamental" if name == "fundamental" else "membership")
        entry = {
            "category": "quantitative" if name != "wyckoff" else "wyckoff_structure",
            "rule_id": f"dimension.{name}", "rule_version": QUANT_RULE_VERSION,
            "input_value": score, "weight": weight,
            "contribution": round(score * weight, 4), "affects_formal_score": True,
            **_source(item, source_name),
        }
        if name == "wyckoff":
            short = (item.get("wyckoff") or {}).get("short_term") or {}
            entry["evidence"] = {"event_date": _date_or_none(short.get("event_date")),
                                 "confirmation_date": _date_or_none(short.get("confirmation_date")),
                                 "signal_status": short.get("signal_status") or "unknown"}
        entries.append(entry)
    raw = _number(item.get("raw_composite_score", item.get("composite_score")))
    quality = item.get("data_quality") or {}
    coverage = _number(quality.get("coverage_factor"), 1.0)
    freshness = _number(quality.get("freshness_factor"), 1.0)
    coverage_score = raw * coverage
    adjusted = _number(item.get("quality_adjusted_score"), coverage_score * freshness)
    entries.extend([
        {"category": "subtotal", "rule_id": "raw_composite", "rule_version": QUANT_RULE_VERSION,
         "input_value": raw, "contribution": raw, "affects_formal_score": True,
         "data_provider": "scanner", "data_date": None, "fetched_at": "unknown", "quality": "derived"},
        {"category": "quality_adjustment", "rule_id": "coverage_factor", "rule_version": QUANT_RULE_VERSION,
         "input_value": coverage, "before": raw, "after": round(coverage_score, 4),
         "contribution": round(coverage_score - raw, 4), "affects_formal_score": True,
         "data_provider": "data_quality", "data_date": _date_or_none(quality.get("as_of_date")), "fetched_at": "unknown", "quality": quality.get("quality") or "derived"},
        {"category": "quality_adjustment", "rule_id": "freshness_factor", "rule_version": QUANT_RULE_VERSION,
         "input_value": freshness, "before": round(coverage_score, 4), "after": adjusted,
         "contribution": round(adjusted - coverage_score, 4), "affects_formal_score": True,
         "data_provider": "data_quality", "data_date": _date_or_none(quality.get("as_of_date")), "fetched_at": "unknown", "quality": quality.get("quality") or "derived"},
    ])
    return {"schema_version": SCHEMA_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
            "entries": entries, "formal_score": adjusted, "formal_priority_score": None,
            "news_shadow_priority_score": None}


def ensure_score_ledger(item):
    item["score_ledger"] = build_quantitative_ledger(item)
    return item


def append_wyckoff_bonus(item, bonus, evidence):
    ledger = item.setdefault("score_ledger", build_quantitative_ledger(item))
    base = _number(item.get("quality_adjusted_score"))
    formal = _number(item.get("execution_priority_score"), base + _number(bonus))
    evidence_copy = copy.deepcopy(evidence)
    for field in ("event_date", "confirmation_date"):
        evidence_copy[field] = _date_or_none(evidence_copy.get(field))
    ledger["entries"].append({
        "category": "wyckoff_buy_point", "rule_id": "strict_buy_point_bonus",
        "rule_version": WYCKOFF_RULE_VERSION, "input_value": _number(bonus),
        "before": base, "after": formal, "contribution": round(formal - base, 4),
        "affects_formal_score": True, "data_provider": "kline", "data_date": _date_or_none(evidence.get("confirmation_date")),
        "fetched_at": "unknown", "quality": "strategy_setting", "evidence": evidence_copy,
    })
    ledger["formal_score"] = base
    ledger["formal_priority_score"] = formal
    return ledger


def append_news_overlay(item, analysis):
    ledger = item.setdefault("score_ledger", build_quantitative_ledger(item))
    base = _number(item.get("execution_priority_score", item.get("quality_adjusted_score")))
    score = _number(analysis.get("score"))
    shadow = _number(analysis.get("shadow_priority_score"), base + score)
    for article in analysis.get("articles") or []:
        ledger["entries"].append({
            "category": "news", "rule_id": f"news.{article.get('label', 'neutral')}",
            "rule_version": NEWS_RULE_VERSION, "input_value": article.get("base_score", article.get("score", 0)),
            "source_weight": article.get("source_factor", 1.0), "time_weight": article.get("time_factor", 1.0),
            "contribution": article.get("decayed_score", 0), "affects_formal_score": False,
            "data_provider": article.get("source") or "unknown", "data_date": _date_or_none(article.get("published_at")),
            "fetched_at": article.get("fetched_at") or "unknown", "quality": article.get("source_tier") or "unknown",
            "evidence_id": article.get("event_id") or "unknown", "url": article.get("url") or "",
        })
    ledger["entries"].append({
        "category": "news_shadow", "rule_id": "news.net_capped_overlay", "rule_version": NEWS_RULE_VERSION,
        "input_value": score, "before": base, "after": shadow, "contribution": round(shadow - base, 4),
        "affects_formal_score": False, "data_provider": "candidate_news", "data_date": _date_or_none(analysis.get("cutoff")),
        "fetched_at": analysis.get("fetched_at") or "unknown", "quality": analysis.get("status") or "unknown",
        "evidence": {"cap": "[-3,+1]", "shadow_veto": bool(analysis.get("shadow_veto"))},
    })
    if ledger.get("formal_priority_score") is None:
        ledger["formal_priority_score"] = base
    ledger["news_shadow_priority_score"] = shadow
    return ledger
