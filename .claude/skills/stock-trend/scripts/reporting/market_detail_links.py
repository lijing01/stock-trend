"""Offline detail links for the frozen daily market-review components."""

from html import escape
import math
from urllib.parse import urlparse


START = "<!-- MARKET_DETAIL_LINKS:START -->"
END = "<!-- MARKET_DETAIL_LINKS:END -->"
MARKET_DETAIL_LINKS_START = START
MARKET_DETAIL_LINKS_END = END

COMPONENTS = (
    ("index_trend", "大盘趋势"),
    ("volume", "成交额"),
    ("breadth", "赚钱效应"),
    ("zt_emotion", "涨停情绪"),
    ("capital", "资金"),
)

_COMPONENT_NAMES = dict(COMPONENTS)
_ALLOWED_HOSTS = {
    "quote.eastmoney.com",
    "data.eastmoney.com",
    "stock.10jqka.com.cn",
}

_SOURCES = {
    "index_trend": (
        ("东方财富·上证指数行情", "https://quote.eastmoney.com/q/1.000001.html"),
        ("东方财富·沪深300行情", "https://quote.eastmoney.com/q/1.000300.html"),
        ("东方财富·深证成指行情", "https://quote.eastmoney.com/q/0.399001.html"),
    ),
    "volume": (
        ("同花顺·市场温度计（两市成交栏目）", "https://stock.10jqka.com.cn/wenduji/"),
        ("东方财富·上证指数行情", "https://quote.eastmoney.com/q/1.000001.html"),
    ),
    "breadth": (
        (
            "东方财富·沪深京A股行情（按涨跌幅查看）",
            "https://quote.eastmoney.com/center/gridlist.html#hs_a_board",
        ),
        (
            "东方财富·行业板块行情",
            "https://quote.eastmoney.com/center/gridlist.html#industry_board",
        ),
    ),
    "zt_emotion": (
        ("东方财富·涨停板行情", "https://quote.eastmoney.com/ztb/?from=ztzt"),
    ),
    "capital": (
        ("东方财富·大盘资金流", "https://data.eastmoney.com/zjlx/dpzjlx.html"),
    ),
}

_METHODS = {
    "index_trend": (
        "上证指数（000001.SH）、沪深300（000300.SH）、深证成指（399001.SZ）"
        "分别评分后取有效项平均：收盘在MA20上且MA20向上为100分；"
        "仅收盘在MA20上为60分；收盘在MA20下但MA20向上为40分；否则0分。"
    ),
    "volume": (
        "两市成交额按上证指数（000001.SH）与深证综指（399106.SZ）的成交额相加，"
        "再与最多20个既往有效交易日均额比较。得分为50＋（当日额/均额－1）×150，"
        "限制在0–100分；历史或来源对齐不足时使用降级分并注明。"
    ),
    "breadth": (
        "涨跌家数按东方财富地域板块加总。得分为70×上涨家数/（上涨＋下跌家数）"
        "＋30×行业板块上涨比例（比例取0–1）；"
        "外部平台的股票覆盖范围可能不同。"
    ),
    "zt_emotion": (
        "历史有效样本至少5个时，基础分为50＋（涨停数－最多20个历史样本均值）×1.5；"
        "不足5个时为50＋（涨停数－50）×0.3，基础分限制在0–100。"
        "最高连板至少5板加20分、至少3板加10分、至少2板加5分，最终限制在0–100分。"
    ),
    "capital": (
        "全市场主力净流入按东方财富地域板块加总。得分为50＋净流入（亿元）×0.06，"
        "限制在0–100分；缺失时使用50分并标注不可用。"
    ),
}

_MISSING_DETAIL = {
    "index_trend": "各指数的冻结收盘价、MA20数值及逐项状态未保存。",
    "volume": "冻结两市成交额或20日均额未保存；不会根据四舍五入后的百分比倒算。",
    "breadth": "冻结涨跌家数或行业上涨占比未保存。",
    "zt_emotion": "冻结涨停家数、连板数、最高板或历史基线未保存。",
    "capital": "冻结全市场主力净流入数值未保存。",
}


def _validate_sources():
    for sources in _SOURCES.values():
        for _, url in sources:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
                raise ValueError(f"unapproved market detail URL: {url}")


_validate_sources()


def _format_name(format):
    if format == "md":
        format = "markdown"
    if format not in {"html", "markdown"}:
        raise ValueError("format must be html or markdown")
    return format


def _md_text(value):
    text = str(value if value not in (None, "") else "未保存")
    text = " ".join(text.splitlines())
    for char in ("\\", "`", "*", "_", "[", "]", "<", ">", "#"):
        text = text.replace(char, "\\" + char)
    return text


def _present(value):
    return value is not None and value != ""


def _explanation_component(ctx, key):
    explanation = ctx.get("market_explanation") or {}
    return next(
        (
            item for item in explanation.get("components", [])
            if isinstance(item, dict) and item.get("id") == key
        ),
        {},
    )


def _component_data(ctx, key):
    component = (ctx.get("components") or {}).get(key) or {}
    explained = _explanation_component(ctx, key)
    score = component.get("score")
    if not _present(score):
        score = explained.get("score")
    detail = component.get("detail")
    if not _present(detail):
        detail = explained.get("detail")
    evidence = explained.get("evidence") or {}
    return score, detail, evidence


def _time_text(ctx, evidence):
    basis_date = ctx.get("data_date") or (
        ctx.get("market_explanation") or {}
    ).get("basis_date")
    source_date = evidence.get("data_date")
    source_timestamp = evidence.get("source_timestamp")
    generated_at = ctx.get("generated_at")
    parts = [f"报告依据日：{basis_date}" if _present(basis_date) else "报告依据日：未保存"]
    parts.append(
        f"组件证据日：{source_date}"
        if _present(source_date)
        else "组件证据日：未保存"
    )
    parts.append(
        f"来源事件时间：{source_timestamp}"
        if _present(source_timestamp)
        else "来源事件时间：未保存，无法据此确认事件发生时刻"
    )
    if _present(generated_at):
        parts.append(f"报告生成时间：{generated_at}")
    return "；".join(parts) + "。"


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return str(value)


def _raw_detail(ctx, key):
    component = (ctx.get("components") or {}).get(key) or {}
    if key == "index_trend":
        records = []
        for code in ("000001.SH", "000300.SH", "399001.SZ"):
            item = (ctx.get("indices") or {}).get(code) or {}
            fields = []
            close = _number(item.get("close"))
            ma20 = _number(item.get("ma20"))
            if close is not None:
                fields.append(f"收盘价 {close}")
            else:
                fields.append("收盘价未保存")
            if ma20 is not None:
                fields.append(f"MA20 {ma20}")
            else:
                fields.append("MA20未保存")
            if isinstance(item.get("above_ma20"), bool):
                fields.append("收盘在MA20上方" if item["above_ma20"] else "收盘在MA20下方")
            if isinstance(item.get("ma20_rising"), bool):
                fields.append("MA20向上" if item["ma20_rising"] else "MA20向下")
            records.append(f"{code}：{'，'.join(fields)}")
        return "；".join(records) + "。"
    if key == "volume":
        amount = _number(ctx.get("amount_yi"))
        return (
            f"两市成交额：{amount}亿；" if amount is not None else "两市成交额：未保存；"
        ) + "20日均额：未保存（不根据比例倒算）。"
    if key == "breadth":
        up = _number(component.get("up"))
        down = _number(component.get("down"))
        return (
            f"上涨家数：{up or '未保存'}；下跌家数：{down or '未保存'}；"
            "行业上涨占比的结构化原始值：未保存。"
        )
    if key == "zt_emotion":
        zt = ctx.get("zt") or {}
        return (
            f"涨停家数：{_number(zt.get('count')) or '未保存'}；"
            f"连板家数：{_number(zt.get('streak_count')) or '未保存'}；"
            f"最高板：{_number(zt.get('max_streak')) or '未保存'}；"
            "历史基线：未保存。"
        )
    return "主力净流入的结构化原始字段：未保存；冻结汇总值以本报告说明为准。"


def summary_link(key, format="html"):
    """Return a stable in-report link for one component summary row."""
    format = _format_name(format)
    if key not in _COMPONENT_NAMES:
        raise KeyError(key)
    target = f"market-detail-{key}"
    if format == "html":
        return f'<a class="market-detail-link" href="#{target}">查看详情</a>'
    return f"[查看详情](#{target})"


def _html_sources(key):
    links = []
    for label, url in _SOURCES[key]:
        links.append(
            f'<li><a href="{escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{escape(label)}</a></li>'
        )
    return "<ul>" + "".join(links) + "</ul>"


def _markdown_sources(key):
    return "\n".join(
        f'- <a href="{escape(url, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer">{escape(label)}</a>'
        for label, url in _SOURCES[key]
    )


def _render_html(ctx):
    articles = []
    for key, name in COMPONENTS:
        score, detail, evidence = _component_data(ctx, key)
        score_text = str(score) if _present(score) else "未保存"
        detail_text = str(detail) if _present(detail) else _MISSING_DETAIL[key]
        articles.append(
            f'<article class="market-detail" id="market-detail-{key}">'
            f"<h3>{escape(name)}</h3>"
            f"<p><strong>本报告冻结得分：</strong>{escape(score_text)}</p>"
            f"<p><strong>冻结说明：</strong>{escape(detail_text)}</p>"
            f"<p><strong>冻结原始字段：</strong>{escape(_raw_detail(ctx, key))}</p>"
            f"<p><strong>依据时间：</strong>{escape(_time_text(ctx, evidence))}</p>"
            f"<p><strong>评分口径：</strong>{escape(_METHODS[key])}</p>"
            "<p><strong>财经行情入口：</strong></p>"
            f"{_html_sources(key)}"
            "<p class=\"market-detail-limit\">外部行情页面可能默认显示最新行情或历史缓存，"
            "请核对站内日期；这些页面不展示本项目评分，且不同平台口径可能不同。</p>"
            '<p><a href="#market-component-summary">返回评分表</a></p>'
            "</article>"
        )
    return (
        START
        + '<section class="market-detail-links" aria-labelledby="market-detail-links-title">'
        + "<style>"
        ".market-detail-links{margin-top:20px}"
        ".market-detail-links .market-detail{padding:14px 0;border-bottom:1px solid #e5e7eb;"
        "overflow-wrap:anywhere;scroll-margin-top:16px}"
        ".market-detail-links .market-detail:last-child{border-bottom:0}"
        ".market-detail-links h3{font-size:16px;margin:4px 0 8px}"
        ".market-detail-links p{font-size:14px;line-height:1.65;margin:6px 0}"
        ".market-detail-links ul{font-size:14px;margin:4px 0 8px}"
        ".market-detail-links .market-detail-limit{color:#6b7280}"
        "</style>"
        + '<h2 id="market-detail-links-title">市场评分详情与财经入口</h2>'
        + "".join(articles)
        + "</section>"
        + END
    )


def _render_markdown(ctx):
    lines = [
        START,
        "## 市场评分详情与财经入口",
        "",
    ]
    for key, name in COMPONENTS:
        score, detail, evidence = _component_data(ctx, key)
        score_text = score if _present(score) else "未保存"
        detail_text = detail if _present(detail) else _MISSING_DETAIL[key]
        lines.extend([
            f'<a id="market-detail-{key}"></a>',
            f"### {_md_text(name)}",
            "",
            f"- 本报告冻结得分：{_md_text(score_text)}",
            f"- 冻结说明：{_md_text(detail_text)}",
            f"- 冻结原始字段：{_md_text(_raw_detail(ctx, key))}",
            f"- 依据时间：{_md_text(_time_text(ctx, evidence))}",
            f"- 评分口径：{_md_text(_METHODS[key])}",
            "- 财经行情入口：",
            _markdown_sources(key),
            "",
            "外部行情页面可能默认显示最新行情或历史缓存，请核对站内日期；"
            "这些页面不展示本项目评分，且不同平台口径可能不同。",
            "",
            '<a href="#market-component-summary">返回评分表</a>',
            "",
        ])
    lines.append(END)
    return "\n".join(lines)


def render_details(ctx, format="html"):
    """Render frozen component details without file or network access."""
    if not isinstance(ctx, dict):
        raise TypeError("ctx must be a dict")
    format = _format_name(format)
    return _render_html(ctx) if format == "html" else _render_markdown(ctx)
