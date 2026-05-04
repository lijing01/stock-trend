#!/usr/bin/env python3
"""Generate ECharts-based K-line chart HTML fragment for stock-trend skill.

Reads kline.json (OHLCV + MA data) and optional technical.json (support/resistance),
transforms data into ECharts option JSON, and outputs an HTML <script> fragment.

Usage:
    python3 generate_chart_html.py /tmp/kline.json -o /tmp/chart_fragment.html
    python3 generate_chart_html.py /tmp/kline.json --technical /tmp/technical.json -o /tmp/chart_fragment.html
"""

import argparse
import json
import sys
from datetime import datetime


def load_kline(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_technical(path):
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compute_ma_series(records, periods=(5, 10, 20, 60)):
    """Compute full MA series from close prices. Used when fetch source doesn't provide MAs."""
    closes = [r.get("close") for r in records]
    ma_data = {f"ma{p}": [] for p in periods}
    for p in periods:
        for i in range(len(closes)):
            if i < p - 1 or any(c is None for c in closes[i - p + 1 : i + 1]):
                ma_data[f"ma{p}"].append(None)
            else:
                window = [c for c in closes[i - p + 1 : i + 1] if c is not None]
                if len(window) >= p:
                    ma_data[f"ma{p}"].append(round(sum(window) / p, 4))
                else:
                    ma_data[f"ma{p}"].append(None)
    return ma_data


def transform_for_echarts(kline_data, technical_data=None, max_bars=120):
    """Transform kline JSON into ECharts-compatible data arrays."""
    records = kline_data.get("data", [])
    if not records:
        return None

    # Truncate to last N bars
    if len(records) > max_bars:
        records = records[-max_bars:]

    dates = []
    ohlc = []       # [open, close, low, high] — ECharts candlestick convention
    volumes = []
    volume_colors = []  # 'up' or 'down' for coloring

    # Check if MA columns exist in data
    has_ma = any(k.startswith("ma") for k in records[0].keys())
    ma_periods = [5, 10, 20, 60]
    ma_series = {f"ma{p}": [] for p in ma_periods}

    for r in records:
        d = r.get("trade_date", "")
        # Format date: 20260504 -> 2026-05-04
        if len(d) == 8 and d.isdigit():
            d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        dates.append(d)

        o = r.get("open")
        c = r.get("close")
        l = r.get("low")
        h = r.get("high")
        v = r.get("vol")

        ohlc.append([o, c, l, h])
        volumes.append(v)
        volume_colors.append("up" if c >= o else "down")

        # Collect MA values from data or mark None
        for p in ma_periods:
            key = f"ma{p}"
            if has_ma and key in r and r[key] is not None:
                ma_series[key].append(r[key])
            else:
                ma_series[key].append(None)

    # If no MA in data, compute from close prices
    if not has_ma:
        all_records = kline_data.get("data", [])
        if len(all_records) > max_bars:
            # Need all records to compute MA properly, then truncate
            computed = compute_ma_series(all_records, ma_periods)
            for key in computed:
                ma_series[key] = computed[key][-max_bars:]
        else:
            ma_series = compute_ma_series(all_records, ma_periods)

    # Build markLines from support/resistance
    mark_lines = []
    if technical_data:
        summary = technical_data.get("summary", {})
        for level in summary.get("support_levels", []):
            if level is not None:
                mark_lines.append({
                    "yAxis": round(level, 2),
                    "name": f"支撑 {round(level, 2)}",
                    "lineStyle": {"color": "#26a69a", "type": "dashed", "width": 1},
                })
        for level in summary.get("resistance_levels", []):
            if level is not None:
                mark_lines.append({
                    "yAxis": round(level, 2),
                    "name": f"压力 {round(level, 2)}",
                    "lineStyle": {"color": "#ef5350", "type": "dashed", "width": 1},
                })

    # Build ECharts option
    ma_colors = {"ma5": "#e6a23c", "ma10": "#409eff", "ma20": "#f56c6c", "ma60": "#909399"}
    ma_line_series = []
    for p in ma_periods:
        key = f"ma{p}"
        ma_line_series.append({
            "name": f"MA{p}",
            "type": "line",
            "data": ma_series[key],
            "smooth": True,
            "lineStyle": {"width": 1, "color": ma_colors.get(key, "#999")},
            "symbol": "none",
            "xAxisIndex": 0,
            "yAxisIndex": 0,
        })

    option = {
        "animation": False,
        "tooltip": {
            "trigger": "axis",
            "axisPointer": {"type": "cross", "crossStyle": {"color": "#999"}},
            "formatter": None,  # Will use JS-side formatter
        },
        "axisPointer": {"link": [{"xAxisIndex": "all"}]},
        "grid": [
            {"left": "8%", "right": "4%", "top": "6%", "height": "58%"},
            {"left": "8%", "right": "4%", "top": "70%", "height": "18%"},
        ],
        "xAxis": [
            {
                "type": "category",
                "data": dates,
                "boundaryGap": True,
                "axisLine": {"lineStyle": {"color": "#777"}},
                "axisLabel": {"color": "#ccc", "fontSize": 10},
                "splitLine": {"show": False},
            },
            {
                "type": "category",
                "gridIndex": 1,
                "data": dates,
                "boundaryGap": True,
                "axisLine": {"lineStyle": {"color": "#777"}},
                "axisLabel": {"show": False},
                "splitLine": {"show": False},
            },
        ],
        "yAxis": [
            {
                "scale": True,
                "splitArea": {"show": True, "areaStyle": {"color": ["#1a1a2e", "#16213e"]}},
                "axisLine": {"lineStyle": {"color": "#777"}},
                "axisLabel": {"color": "#ccc", "fontSize": 10},
                "splitLine": {"lineStyle": {"color": "#2a2a3e"}},
            },
            {
                "scale": True,
                "gridIndex": 1,
                "splitNumber": 2,
                "axisLine": {"lineStyle": {"color": "#777"}},
                "axisLabel": {"color": "#ccc", "fontSize": 10, "formatter": "{value}"},
                "splitLine": {"lineStyle": {"color": "#2a2a3e"}},
            },
        ],
        "dataZoom": [
            {
                "type": "inside",
                "xAxisIndex": [0, 1],
                "start": max(0, 100 - (60 / len(dates)) * 100) if len(dates) > 60 else 0,
                "end": 100,
            },
            {
                "type": "slider",
                "xAxisIndex": [0, 1],
                "bottom": "2%",
                "height": 14,
                "borderColor": "#444",
                "fillerColor": "rgba(64,158,255,0.2)",
                "handleStyle": {"color": "#409eff"},
                "textStyle": {"color": "#ccc", "fontSize": 10},
                "start": max(0, 100 - (60 / len(dates)) * 100) if len(dates) > 60 else 0,
                "end": 100,
            },
        ],
        "series": [
            {
                "name": "K线",
                "type": "candlestick",
                "data": ohlc,
                "itemStyle": {
                    "color": "#ef5350",       # up candle fill (red=up, Chinese convention)
                    "color0": "#26a69a",      # down candle fill (green=down)
                    "borderColor": "#ef5350", # up candle border
                    "borderColor0": "#26a69a", # down candle border
                },
                "xAxisIndex": 0,
                "yAxisIndex": 0,
            }
        ] + ma_line_series + [
            {
                "name": "成交量",
                "type": "bar",
                "xAxisIndex": 1,
                "yAxisIndex": 1,
                "data": volumes,
                "itemStyle": {
                    "color": "#ef5350",  # red for up days
                    "color0": "#26a69a", # green for down days
                },
            },
        ],
    }

    # Add markLines for support/resistance on the candlestick series
    if mark_lines:
        option["series"][0]["markLine"] = {
            "symbol": "none",
            "data": mark_lines,
            "label": {"color": "#ccc", "fontSize": 10},
            "animation": False,
        }

    return option


def build_chart_html(option, kline_data):
    """Build the complete HTML <script> fragment for the chart."""
    ts_code = kline_data.get("meta", {}).get("ts_code", "")
    data_source = kline_data.get("meta", {}).get("data_source", "")
    record_count = kline_data.get("meta", {}).get("record_count", 0)

    # Custom tooltip formatter as JS function
    tooltip_js = """
function(params) {
    var idx = params[0].dataIndex;
    var date = params[0].axisValue;
    var result = '<div style="font-size:12px">';
    result += '<div style="margin-bottom:4px;font-weight:bold">' + date + '</div>';
    for (var i = 0; i < params.length; i++) {
        var s = params[i];
        if (s.seriesType === 'candlestick') {
            var d = s.data;
            result += '<div>' + s.seriesName + ': 开' + d[1] + ' 收' + d[2] + ' 低' + d[3] + ' 高' + d[4] + '</div>';
        } else if (s.seriesType === 'bar') {
            result += '<div>' + s.seriesName + ': ' + (s.data != null ? s.data.toFixed(0) : '-') + '</div>';
        } else if (s.seriesType === 'line' && s.data != null) {
            result += '<div>' + s.seriesName + ': ' + s.data.toFixed(2) + '</div>';
        }
    }
    result += '</div>';
    return result;
}"""

    option_json = json.dumps(option, ensure_ascii=False, indent=2)
    # Inject the tooltip formatter (can't serialize JS functions in JSON)
    option_json = option_json.replace(
        '"formatter": null',
        '"formatter": ' + tooltip_js.strip()
    )

    # Volume bar coloring needs per-item color based on candle direction
    # We build a JS expression to set colors dynamically
    records = kline_data.get("data", [])
    max_bars = 120
    if len(records) > max_bars:
        records = records[-max_bars:]
    vol_colors_js = "["
    for r in records:
        c = r.get("close", 0)
        o = r.get("open", 0)
        vol_colors_js += "'#ef5350'," if c >= o else "'#26a69a',"
    vol_colors_js += "]"

    html = f"""<div id="kline-chart" style="width:100%;height:560px;"></div>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js" onerror="document.getElementById('kline-chart').innerHTML='<p style=\\'color:#999;text-align:center;padding:40px\\'>图表加载需要网络连接 (ECharts CDN)</p>'"></script>
<script>
(function() {{
    var chartDom = document.getElementById('kline-chart');
    if (typeof echarts === 'undefined') return;
    var chart = echarts.init(chartDom, 'dark');
    var option = {option_json};

    // Apply per-bar volume colors
    var volColors = {vol_colors_js};
    var volSeries = option.series.find(function(s) {{ return s.name === '成交量'; }});
    if (volSeries && volColors.length) {{
        volSeries.itemStyle.color = function(params) {{
            return volColors[params.dataIndex] || '#999';
        }};
    }}

    chart.setOption(option);
    window.addEventListener('resize', function() {{ chart.resize(); }});

    // Data source annotation
    var info = document.createElement('div');
    info.style.cssText = 'text-align:center;color:#666;font-size:11px;margin-top:4px';
    info.textContent = '数据来源: {data_source} | {record_count} 条记录 | {ts_code}';
    chartDom.parentNode.appendChild(info);
}})();
</script>"""

    return html


def main():
    parser = argparse.ArgumentParser(description="Generate ECharts K-line chart HTML fragment")
    parser.add_argument("kline_file", help="K-line JSON file from fetch_kline.py")
    parser.add_argument("--technical", help="Technical analysis JSON file (optional, for support/resistance levels)")
    parser.add_argument("--max-bars", type=int, default=120, help="Max number of bars to display (default: 120)")
    parser.add_argument("-o", "--output", help="Output HTML file path (default: stdout)")

    args = parser.parse_args()

    kline_data = load_kline(args.kline_file)
    technical_data = load_technical(args.technical)

    # Check for error
    if kline_data.get("meta", {}).get("data_source") == "error" or not kline_data.get("data"):
        html = '<div id="kline-chart" style="width:100%;padding:20px;text-align:center;color:#999;">K线数据不可用，图表无法生成</div>'
    else:
        option = transform_for_echarts(kline_data, technical_data, args.max_bars)
        if option is None:
            html = '<div id="kline-chart" style="width:100%;padding:20px;text-align:center;color:#999;">K线数据为空，图表无法生成</div>'
        else:
            html = build_chart_html(option, kline_data)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Chart HTML written to {args.output}", file=sys.stderr)
    else:
        print(html)


if __name__ == "__main__":
    main()