"""Post-selection news evidence for daily candidates.

The overlay is deliberately a shadow model: it records what a news-aware
ordering would have done, but it never changes the formal recommendation.  A
later evaluator can therefore compare both choices without look-ahead or an
unreviewed NLP rule silently changing production decisions.
"""
import copy
import hashlib
import re
from datetime import date, datetime, time, timedelta, timezone


SCHEMA_VERSION = "candidate-news-overlay/v1"
SHANGHAI = timezone(timedelta(hours=8))

_CRITICAL = (
    "立案调查", "涉嫌违法", "终止上市", "退市风险", "暂停上市",
    "重大违法", "债务违约", "破产重整", "无法表示意见", "否定意见",
)
_NEGATIVE = (
    "业绩预亏", "业绩下滑", "净利润下降", "亏损", "减持", "质押",
    "处罚", "警示函", "监管函", "问询函", "诉讼", "仲裁", "停产",
    "事故", "召回", "解禁", "商誉减值", "控制权变更风险", "风险提示",
)
_POSITIVE = (
    "业绩预增", "净利润增长", "扭亏为盈", "增持", "回购", "中标",
    "签订合同", "重大合同", "分红", "订单", "获得批准", "产能投产",
)
_UNCERTAIN = ("澄清", "传闻", "拟", "意向", "框架协议", "尚存在不确定性")
_OFFICIAL_SOURCES = ("巨潮", "上交所", "深交所", "港交所", "公司公告")
_NEGATIVE_NEGATIONS = (
    "撤销退市风险警示", "申请撤销退市风险警示", "解除质押",
    "不减持", "终止减持计划", "减持计划实施完毕",
)


def _parse_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        parsed = None
        for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                parsed = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def decision_cutoff(recommendation_date, provisional=False):
    """Return the latest timestamp that may inform this recommendation."""
    day = date.fromisoformat(recommendation_date)
    # Intraday callers should supply an explicit earlier cutoff in the input
    # envelope.  This fallback remains conservative and never includes news
    # published after the formal close decision.
    return datetime.combine(day, time(15, 0), tzinfo=SHANGHAI)


def _source_tier(source, url=""):
    text = f"{source} {url}".lower()
    if any(name.lower() in text for name in _OFFICIAL_SOURCES) or \
            any(host in text for host in ("cninfo.com.cn", "sse.com.cn", "szse.cn", "hkexnews.hk")):
        return "official"
    return "media"


def _term_hits(text, terms):
    return [term for term in terms if term in text]


def classify_article(item):
    """Classify one title/summary with auditable, intentionally small rules."""
    title = str(item.get("title") or item.get("新闻标题") or item.get("公告标题") or "").strip()
    summary = str(item.get("summary") or item.get("content") or item.get("新闻内容") or "").strip()
    text = re.sub(r"\s+", " ", f"{title} {summary}")
    source = str(item.get("source") or item.get("文章来源") or "")
    url = str(item.get("url") or item.get("新闻链接") or item.get("公告链接") or "")
    tier = _source_tier(source, url)
    risk_text = text
    for phrase in _NEGATIVE_NEGATIONS:
        risk_text = risk_text.replace(phrase, "")
    critical = _term_hits(risk_text, _CRITICAL)
    negative = _term_hits(risk_text, _NEGATIVE)
    positive = _term_hits(text, _POSITIVE)
    uncertain = _term_hits(text, _UNCERTAIN)
    if critical:
        label, score, risk = "critical_negative", -3.0, "critical"
    elif negative and not positive:
        label, score, risk = "negative", -1.5, "high" if tier == "official" else "medium"
    elif positive and not negative:
        label, score, risk = "positive", 0.75, "none"
    elif positive or negative:
        label, score, risk = "mixed", -0.5 if negative else 0.25, "medium" if negative else "none"
    else:
        label, score, risk = "neutral", 0.0, "none"
    if uncertain and score > 0:
        score = min(score, 0.25)
        label = "uncertain_positive"
    if tier != "official":
        score *= 0.6
    return {
        "title": title,
        "published_at": item.get("published_at") or item.get("发布时间") or item.get("公告时间"),
        "source": source or ("巨潮资讯" if "cninfo.com.cn" in url else "unknown"),
        "source_tier": tier,
        "url": url,
        "label": label,
        "score": round(score, 2),
        "risk_level": risk,
        "matched_terms": {"critical": critical, "negative": negative,
                          "positive": positive, "uncertain": uncertain},
    }


def evaluate_candidate_news(items, *, cutoff, lookback_days=14):
    cutoff = _parse_datetime(cutoff)
    if cutoff is None:
        raise ValueError("invalid_news_cutoff")
    start = cutoff - timedelta(days=lookback_days)
    eligible = []
    excluded = {"future": 0, "too_old": 0, "unknown_time": 0, "duplicate": 0}
    seen = set()
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        article = classify_article(raw)
        published = _parse_datetime(article["published_at"])
        if published is None:
            excluded["unknown_time"] += 1
            continue
        if published > cutoff:
            excluded["future"] += 1
            continue
        if published < start:
            excluded["too_old"] += 1
            continue
        fingerprint = hashlib.sha256(
            (article["title"] + published.isoformat()).encode("utf-8")
        ).hexdigest()[:16]
        if fingerprint in seen:
            excluded["duplicate"] += 1
            continue
        seen.add(fingerprint)
        age_days = max(0.0, (cutoff - published).total_seconds() / 86400)
        decay = max(0.25, 1 - age_days / max(lookback_days, 1))
        article["published_at"] = published.isoformat()
        article["age_days"] = round(age_days, 2)
        article["decayed_score"] = round(article["score"] * decay, 3)
        eligible.append(article)
    eligible.sort(key=lambda row: row["published_at"], reverse=True)
    score = sum(row["decayed_score"] for row in eligible)
    critical = any(row["risk_level"] == "critical" for row in eligible)
    high = any(row["risk_level"] == "high" for row in eligible)
    # A recent formal critical disclosure is a categorical shadow veto; time
    # decay must not make the same event appear less severe within the window.
    score = -3.0 if critical else round(max(-3.0, min(1.0, score)), 2)
    status = "ready" if eligible else "no_recent_news"
    return {
        "status": status,
        "cutoff": cutoff.isoformat(),
        "lookback_days": lookback_days,
        "article_count": len(eligible),
        "score": score,
        "risk_level": "critical" if critical else "high" if high else "none",
        "shadow_veto": critical,
        "excluded": excluded,
        "articles": eligible[:8],
    }


def apply_news_overlay(candidates, evidence_by_code, *, recommendation_date,
                       policy=None, cutoff=None, lookback_days=14):
    """Annotate candidates and return a non-production shadow comparison."""
    cutoff = cutoff or decision_cutoff(
        recommendation_date, bool((policy or {}).get("provisional")))
    annotated = []
    missing = 0
    for candidate in candidates or []:
        item = copy.deepcopy(candidate)
        code = str(item.get("code") or "")
        raw = (evidence_by_code or {}).get(code)
        if raw is None:
            analysis = {
                "status": "unavailable", "cutoff": _parse_datetime(cutoff).isoformat(),
                "lookback_days": lookback_days, "article_count": 0,
                "score": 0.0, "risk_level": "unknown", "shadow_veto": False,
                "excluded": {}, "articles": [],
            }
            missing += 1
        else:
            analysis = evaluate_candidate_news(raw, cutoff=cutoff, lookback_days=lookback_days)
        base = float(item.get("execution_priority_score", item.get("quality_adjusted_score", 0)) or 0)
        analysis["shadow_priority_score"] = round(max(0, min(100, base + analysis["score"])), 2)
        analysis["formal_policy_affected"] = False
        item["news_analysis"] = analysis
        annotated.append(item)
    baseline = [str(item.get("code")) for item in annotated]
    ranked = sorted(annotated, key=lambda row: (
        bool((row.get("news_analysis") or {}).get("shadow_veto")),
        -float((row.get("news_analysis") or {}).get("shadow_priority_score", 0)),
        baseline.index(str(row.get("code"))),
    ))
    limit = int((policy or {}).get("max_recommendations", 0) or 0)
    def hard_gate_pass(row):
        quality = row.get("data_quality") or {}
        short_status = ((row.get("wyckoff") or {}).get("short_term") or {}).get("signal_status")
        capital_ok = (not (policy or {}).get("requires_sector_capital_proof")
                      or row.get("sector_capital_evidence") == "positive_verified")
        return (quality.get("eligible") is True
                and row.get("sector_actionable", True)
                and row.get("score_eligible", True)
                and short_status not in {"retest_pending", "failed_breakout"}
                and capital_ok)

    shadow_selected = [str(row.get("code")) for row in ranked
                       if hard_gate_pass(row)
                       and not row["news_analysis"]["shadow_veto"]][:limit]
    shadow_selected_set = set(shadow_selected)
    for row in annotated:
        row["news_analysis"]["shadow_selected"] = (
            str(row.get("code")) in shadow_selected_set)
    return annotated, {
        "schema_version": SCHEMA_VERSION,
        "status": "ready" if annotated and missing == 0 else "partial" if annotated else "empty",
        "recommendation_date": recommendation_date,
        "cutoff": _parse_datetime(cutoff).isoformat(),
        "lookback_days": lookback_days,
        "formal_policy_affected": False,
        "baseline_order": baseline,
        "shadow_order": [str(row.get("code")) for row in ranked],
        "shadow_selected": shadow_selected,
        "missing_candidates": missing,
        "method": "official-first keyword rules; score cap [-3,+1]; critical risk shadow veto",
    }


def load_news_evidence(payload):
    """Accept {records:{code:[...]}} or a direct code-to-items mapping."""
    if not isinstance(payload, dict):
        raise ValueError("news evidence must be an object")
    records = payload.get("records", payload)
    if not isinstance(records, dict):
        raise ValueError("news evidence records must be an object")
    return {str(code): list(items or []) for code, items in records.items()
            if isinstance(items, list)}
