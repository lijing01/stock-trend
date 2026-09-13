"""Pure candidate ranking keys and concentration diagnostics."""


def candidate_quality_score(item):
    return float(item.get("quality_adjusted_score", item.get("composite_score", 0)) or 0)


def candidate_rank_score(item):
    return float(item.get("execution_priority_score", candidate_quality_score(item)) or 0)


def candidate_concentration(candidates, top_n=10):
    unique, seen = [], set()
    for item in candidates:
        code = str(item.get("code") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        unique.append(item)
        if len(unique) >= top_n:
            break
    counts, cross_exposure = {}, 0
    for item in unique:
        primary = item.get("sector_name") or item.get("sector_code") or "未知板块"
        counts[primary] = counts.get(primary, 0) + 1
        if len(item.get("sector_memberships") or []) > 1:
            cross_exposure += 1
    total = len(unique)
    distribution = [
        {"sector": sector, "count": count, "share": round(count / total, 4)}
        for sector, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ] if total else []
    return {
        "sample_size": total, "top_n": top_n, "by_primary_sector": distribution,
        "max_primary_sector_share": distribution[0]["share"] if distribution else None,
        "cross_sector_exposure_count": cross_exposure,
        "theme_correlation": "unknown_no_reliable_same_period_return_mapping",
        "note": "按代码去重并仅计主归属；多板块归属单列交叉暴露，不改变候选排序或资格。",
    }
