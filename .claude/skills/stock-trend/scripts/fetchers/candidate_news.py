"""Live candidate news adapter (CNINFO disclosures first, media second)."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache


CNINFO_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
}


def _rows(frame, mapping):
    result = []
    if frame is None:
        return result
    for row in frame.to_dict("records"):
        result.append({target: row.get(source) for target, source in mapping.items()})
    return result


@lru_cache(maxsize=1)
def _cninfo_stock_ids():
    import requests
    try:
        response = requests.get(
            "http://www.cninfo.com.cn/new/data/szse_stock.json",
            headers=CNINFO_HEADERS, timeout=10)
        response.raise_for_status()
        return {str(row.get("code")): row.get("orgId")
                for row in response.json().get("stockList", []) if row.get("code")}
    except Exception:
        # Cache the failed bootstrap as an empty mapping for this process so a
        # 30-stock scan does not hammer the same unavailable endpoint.
        return {}


def _fetch_cninfo(code, start, end):
    """Query CNINFO directly; avoids an AKShare 1.18 mapping-argument bug."""
    import requests
    org_id = _cninfo_stock_ids().get(str(code))
    if not org_id:
        raise KeyError("cninfo_org_id_missing")
    payload = {
        "pageNum": "1", "pageSize": "30", "column": "szse",
        "tabName": "relation", "plate": "", "stock": f"{code},{org_id}",
        "searchkey": "", "secid": "", "category": "", "trade": "",
        "seDate": f"{start.isoformat()}~{end.isoformat()}",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    response = requests.post(
        "http://www.cninfo.com.cn/new/hisAnnouncement/query",
        data=payload, headers=CNINFO_HEADERS, timeout=12)
    response.raise_for_status()
    rows = []
    for item in response.json().get("announcements") or []:
        stamp = item.get("announcementTime")
        published = None
        if isinstance(stamp, (int, float)):
            published = datetime.fromtimestamp(
                stamp / 1000, tz=timezone.utc).astimezone().isoformat()
        announcement_id = item.get("announcementId")
        rows.append({
            "title": item.get("announcementTitle"),
            "published_at": published,
            "source": "巨潮资讯/公司公告",
            "url": ("http://www.cninfo.com.cn/new/disclosure/detail"
                    f"?stockCode={code}&announcementId={announcement_id}&orgId={org_id}"),
        })
    return rows


def fetch_one(code, *, end_date, lookback_days=14):
    end = date.fromisoformat(end_date)
    start = end - timedelta(days=lookback_days)
    items, errors = [], []
    try:
        items.extend(_fetch_cninfo(str(code), start, end))
    except Exception as exc:
        errors.append("cninfo:" + type(exc).__name__)
    try:
        import akshare as ak
        frame = ak.stock_news_em(symbol=str(code))
        items.extend(_rows(frame, {
            "title": "新闻标题", "summary": "新闻内容",
            "published_at": "发布时间", "source": "文章来源", "url": "新闻链接",
        }))
    except Exception as exc:
        errors.append("news_em:" + type(exc).__name__)
    return str(code), items, errors


def fetch_candidate_news(codes, *, end_date, lookback_days=14, workers=6):
    evidence, errors = {}, {}
    unique = list(dict.fromkeys(str(code) for code in codes if code))
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(unique) or 1))) as pool:
        futures = {pool.submit(fetch_one, code, end_date=end_date,
                               lookback_days=lookback_days): code for code in unique}
        for future in as_completed(futures):
            code = futures[future]
            try:
                _, items, failures = future.result()
                # When both providers failed, absence is unknown rather than
                # evidence that the company had no recent news.
                if items or len(failures) < 2:
                    evidence[code] = items
                if failures:
                    errors[code] = failures
            except Exception as exc:
                errors[code] = [type(exc).__name__]
    return evidence, {"status": "ready" if not errors else "partial",
                      "provider_errors": errors, "requested": len(unique),
                      "returned": len(evidence)}
