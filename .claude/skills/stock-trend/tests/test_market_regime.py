#!/usr/bin/env python3
"""market_regime (/daily-review) test suite.

Tests for analysis/market_regime.py covering:
  - score_index_trend: MA20 状态矩阵 + 数据缺失降级
  - score_volume: 成交额 vs 20日均额 + 缺失降级
  - score_breadth: 涨跌家数比 + 缺失降级
  - score_zt_emotion: 历史均值 + 连板加成 + 无历史
  - score_capital: 北向 + 主力降级 + 双缺失
  - compute_regime: 加权/钳制/gate 三档
  - build_plan: 三档 if-then + 持仓信号
  - generate_report: 五段渲染
  - _index_metrics: MA 计算
  - 持久化: history prune

Usage:
    python3 test_market_regime.py              # Run all tests
    python3 test_market_regime.py -v           # Verbose
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).parent
SCRIPTS_DIR = SCRIPT_DIR.parent / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

from analysis import market_regime as mr
from fetchers import kline_eastmoney as ke

PASSED = 0
FAILED = 0
SKIPPED = 0
RESULTS = []


def test(name, condition, detail="", category="market_regime"):
    global PASSED, FAILED, SKIPPED
    if condition:
        PASSED += 1
        status = "PASS"
    else:
        FAILED += 1
        status = "FAIL"
    RESULTS.append({"name": name, "status": status, "detail": detail, "category": category})
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def skip(name, reason=""):
    global SKIPPED
    SKIPPED += 1
    RESULTS.append({"name": name, "status": "SKIP", "detail": reason, "category": "skip"})
    print(f"  [SKIP] {name}" + (f" — {reason}" if reason else ""))


def make_metrics(above_ma20=True, ma20_rising=True, close=100.0, pct=0.5):
    return {"ok": True, "close": close, "ma5": close * 0.99, "ma20": close * 0.98,
            "ma20_rising": ma20_rising, "above_ma20": above_ma20, "pct_chg": pct}


# ──────────────── score_index_trend ────────────────


def test_index_trend():
    print("\n--- score_index_trend ---")
    # 全上MA20且向上 → 100
    m = {c: make_metrics(True, True) for c in ["000001.SH", "000300.SH", "399001.SZ"]}
    r = mr.score_index_trend(m)
    test("全上MA20↑=100", r["score"] == 100.0, f"got {r['score']}")

    # 全下MA20且向下 → 0
    m = {c: make_metrics(False, False) for c in ["000001.SH", "000300.SH", "399001.SZ"]}
    r = mr.score_index_trend(m)
    test("全下MA20↓=0", r["score"] == 0.0, f"got {r['score']}")

    # 混合: 上↑(100) + 下↑(40) → 70
    m = {"a": make_metrics(True, True), "b": make_metrics(False, True)}
    r = mr.score_index_trend(m)
    test("混合(100+40)/2=70", abs(r["score"] - 70.0) < 0.01, f"got {r['score']}")

    # 数据不可用 → 50 中性
    r = mr.score_index_trend({"a": {"ok": False}})
    test("数据缺失=50", r["score"] == 50.0, f"got {r['score']}")


# ──────────────── score_volume ────────────────


def test_volume():
    print("\n--- score_volume ---")
    # 高于均额 → >50; ratio 1.2 → 80
    r = mr.score_volume(1200.0, [1000.0] * 20)
    test("放量>50", r["score"] > 50.0, f"got {r['score']}")
    test("ratio1.2≈80", abs(r["score"] - 80.0) < 0.01, f"got {r['score']}")

    # 缩量 ratio 0.8 → 20
    r = mr.score_volume(800.0, [1000.0] * 20)
    test("缩量ratio0.8≈20", abs(r["score"] - 20.0) < 0.01, f"got {r['score']}")

    # 无今日值 / 历史不足 → 50
    test("无今日=50", mr.score_volume(None, [1000.0])["score"] == 50.0)
    insufficient = mr.score_volume(1000.0, [900.0] * 4)
    test("历史不足=50", insufficient["score"] == 50.0)
    test("历史不足原因", "4/5" in insufficient["detail"], insufficient["detail"])


def test_index_fallback_and_amount():
    print("\n--- index fallback and amount ---")

    quote = [""] * 36
    quote[30] = "20260807161402"
    quote[35] = "3940.04/564988582/1209543573294"
    payload = {
        "code": 0,
        "data": {
            "sh000001": {
                "day": [
                    ["2026-08-06", "3880", "3900", "3910", "3870", "500000000"],
                    ["2026-08-07", "3896", "3940", "3941", "3886", "564988582"],
                ],
                "qt": {"sh000001": quote},
            }
        },
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        records, _ = ke.fetch_tencent_a_stock("000001.SH", "D")
    test("腾讯指数 day 可解析", len(records) == 2, f"records={len(records)}")
    test("腾讯指数成交额写入末日", records[-1]["amount"] == 1209543573294.0,
         f"amount={records[-1].get('amount')}")

    payload["data"]["sh000001"]["qt"]["sh000001"][30] = "20260808161402"
    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        mismatched, _ = ke.fetch_tencent_a_stock("000001.SH", "D")
    test("腾讯快照日期不匹配时不写成交额",
         mismatched[-1]["amount"] == 0, str(mismatched[-1]))

    fallback_records = [
        {"trade_date": f"202607{i + 1:02d}", "close": 3800 + i, "amount": 0}
        for i in range(25)
    ]
    diagnostics = {}
    with patch("fetchers.kline_eastmoney.fetch_eastmoney", side_effect=RuntimeError("em down")), \
            patch("fetchers.kline_eastmoney.fetch_tencent_a_stock",
                  return_value=(fallback_records, "上证指数")), \
            patch("fetchers.kline_eastmoney.fetch_baostock",
                  side_effect=AssertionError("BaoStock should not run")):
        actual = mr.fetch_index_kline("000001.SH", diagnostics=diagnostics)
    test("东财失败后使用腾讯", actual == fallback_records)
    test("诊断记录腾讯来源", diagnostics.get("source") == "tencent",
         json.dumps(diagnostics, ensure_ascii=False))

    history = {
        "2026-08-05": {"amount_yi": 1000},
        "2026-08-06": {"amount_yi": 1100},
        "2026-08-07": {"amount_yi": 9999},
    }
    previous = mr.previous_amounts(history, "2026-08-07")
    test("成交额基准排除当日", previous == [1000.0, 1100.0], str(previous))

    complete = mr.complete_market_amounts({
        "000001.SH": [
            {"trade_date": "20260806", "amount": 1e9},
            {"trade_date": "20260807", "amount": 1.2e9},
        ],
        "399106.SZ": [
            {"trade_date": "20260807", "amount": 1.4e9},
        ],
    })
    test("两市成交额只保留双方完整日期",
         complete == {"20260807": 26.0}, str(complete))

    agent_output = mr.build_agent_output({
        "generated_at": "2026-08-07 16:00:00",
        "data_date": "2026-08-07",
        "regime": {}, "components": {}, "amount_yi": 26.0, "zt": {},
        "top_sectors": [], "bottom_sectors": [], "holdings": [], "plan": [],
        "index_data_quality": {"000001.SH": {"source": "tencent"}},
    })
    test("Agent JSON 暴露指数数据质量",
         agent_output["index_data_quality"]["000001.SH"]["source"] == "tencent")


def test_collect_context_rejects_stale_turnover_leg():
    print("\n--- collect_context stale turnover leg ---")
    trend_rows = [
        {"trade_date": f"202607{i + 1:02d}", "close": 3800 + i,
         "amount": 0, "pct_chg": 0.1}
        for i in range(20)
    ] + [{"trade_date": "20260807", "close": 3900,
          "amount": 0, "pct_chg": 0.2}]
    rows_by_code = {
        "000001.SH": [
            {"trade_date": "20260806", "close": 3880, "amount": 1e9},
            {"trade_date": "20260807", "close": 3900, "amount": 1.2e9},
        ],
        "000300.SH": trend_rows,
        "399001.SZ": trend_rows,
        "399106.SZ": [
            {"trade_date": "20260806", "close": 2500, "amount": 1.4e9},
        ],
    }

    def fake_index(code, lmt=80, retries=2, diagnostics=None):
        rows = rows_by_code[code]
        if diagnostics is not None:
            diagnostics.update({"source": "fixture", "record_count": len(rows),
                                "data_date": rows[-1]["trade_date"], "errors": []})
        return rows

    with patch.object(mr, "fetch_index_kline", side_effect=fake_index), \
            patch.object(mr, "fetch_sector_rankings", return_value=[]), \
            patch.object(mr, "fetch_zt_stats", return_value={"count": 0}), \
            patch.object(mr, "fetch_market_activity", return_value=None), \
            patch.object(mr, "fetch_northbound", return_value=None), \
            patch.object(mr, "load_history", return_value={}), \
            patch.object(mr, "load_portfolio", return_value=[]):
        ctx = mr.collect_context()

    test("单边成交额过期不回退复盘日期",
         ctx["data_date"] == "2026-08-07", ctx["data_date"])
    test("单边成交额过期不冒充今日两市总额",
         ctx["amount_yi"] is None, str(ctx["amount_yi"]))
    test("单边成交额过期保持成交额中性",
         ctx["components"]["volume"]["detail"] == "成交额不可用",
         ctx["components"]["volume"]["detail"])


# ──────────────── score_breadth ────────────────


def test_breadth():
    print("\n--- score_breadth ---")
    # 涨跌家数 750/250 + 板块全涨 → 高分
    sectors = [{"change_pct": 1.0}, {"change_pct": 0.5}, {"change_pct": -0.2}]
    r = mr.score_breadth({"up": 750, "down": 250}, sectors)
    test("涨多分高", r["score"] > 60.0, f"got {r['score']}")
    test("up/down 字段正确", r["up"] == 750 and r["down"] == 250)

    # 缺失 → 50
    r = mr.score_breadth(None, sectors)
    test("breadth缺失=50", r["score"] == 50.0, f"got {r['score']}")


# ──────────────── score_zt_emotion ────────────────


def test_zt():
    print("\n--- score_zt_emotion ---")
    # 高于历史均值 + 高连板 → 高分
    r = mr.score_zt_emotion({"count": 90, "streak_count": 8, "max_streak": 5},
                            [60, 65, 70, 62, 68, 66, 70, 64])
    test("涨停多+高连板>60", r["score"] > 60.0, f"got {r['score']}")

    # 无历史 → 按绝对家数
    r = mr.score_zt_emotion({"count": 99, "streak_count": 10, "max_streak": 9}, [])
    test("无历史按绝对", r["score"] > 50.0, f"got {r['score']}")

    # 连板加成: max_streak 5 → +20
    base = mr.score_zt_emotion({"count": 50, "streak_count": 0, "max_streak": 0}, [60, 62, 58])
    high = mr.score_zt_emotion({"count": 50, "streak_count": 5, "max_streak": 5}, [60, 62, 58])
    test("高连板加成", high["score"] > base["score"], f"base{base['score']} high{high['score']}")


# ──────────────── score_capital ────────────────


def test_capital():
    print("\n--- score_capital ---")
    # 北向净买入 → 高分
    r = mr.score_capital(15.0, None)
    test("北向流入>50", r["score"] > 50.0, f"got {r['score']}")

    # 北向不可用 → 降级主力净流入
    r = mr.score_capital(None, {"main_force_yi": 300.0})
    test("降级主力>50", r["score"] > 50.0, f"got {r['score']}")
    test("降级标记", "降级" in r["detail"], r["detail"])

    # 双缺失 → 50
    r = mr.score_capital(None, None)
    test("双缺失=50", r["score"] == 50.0, f"got {r['score']}")


# ──────────────── compute_regime ────────────────


def test_regime():
    print("\n--- compute_regime ---")
    # 强势: 全 90
    comps = {k: {"score": 90} for k in ["index_trend", "volume", "breadth", "zt_emotion", "capital"]}
    r = mr.compute_regime(comps)
    test("全90=强势", r["label"] == "强势" and r["score"] >= 80, f"got {r['label']} {r['score']}")

    # 弱势: 全 30
    comps = {k: {"score": 30} for k in comps}
    r = mr.compute_regime(comps)
    test("全30=弱势", r["label"] == "弱势", f"got {r['label']}")

    # 中性: 混合 ~70
    comps = {"index_trend": {"score": 70}, "volume": {"score": 70},
             "breadth": {"score": 70}, "zt_emotion": {"score": 70}, "capital": {"score": 70}}
    r = mr.compute_regime(comps)
    test("全70=中性", r["label"] == "中性", f"got {r['label']} {r['score']}")

    # 部分组件缺失 → 权重重分配不卡死
    comps = {"index_trend": {"score": 100}, "breadth": {"score": 100}}
    r = mr.compute_regime(comps)
    test("缺组件不报错", isinstance(r["score"], float) and r["label"] in ("强势", "中性", "弱势"))

    # 全空 → 50
    r = mr.compute_regime({})
    test("全空=50", r["score"] == 50.0, f"got {r['score']}")


# ──────────────── build_plan ────────────────


def test_plan():
    print("\n--- build_plan ---")
    p = mr.build_plan({"label": "强势"}, [])
    test("强势有计划", len(p) >= 2, f"n={len(p)}")
    p = mr.build_plan({"label": "弱势"}, [])
    test("弱势含降仓", any("空仓" in x or "降仓" in x for x in p), "|".join(p))

    # 持仓信号: 破止损 → 离场
    holdings = [{"ok": True, "name": "测试", "close": 10.0, "stop_loss": 10.5,
                 "above_ma20": True, "above_ma5": True, "ma20": 9.5, "ma5": 9.8, "pct_chg": 1.0}]
    p = mr.build_plan({"label": "中性"}, holdings)
    test("破止损提示", any("止损" in x and "离场" in x for x in p), "|".join(p))

    # 破MA20 → 减仓
    holdings = [{"ok": True, "name": "测试", "close": 10.0, "stop_loss": None,
                 "above_ma20": False, "above_ma5": False, "ma20": 10.5, "ma5": 10.3, "pct_chg": -2.0}]
    p = mr.build_plan({"label": "中性"}, holdings)
    test("破MA20提示", any("MA20" in x and "减仓" in x for x in p), "|".join(p))


# ──────────────── generate_report ────────────────


def test_report():
    print("\n--- generate_report ---")
    ctx = {
        "data_date": "2026-07-31",
        "generated_at": "2026-08-01 19:00:00",
        "stale_note": "",
        "regime": {"score": 57.5, "label": "弱势", "advice": "降仓/空仓,不找牛股"},
        "components": {
            "index_trend": {"score": 0.0, "detail": "全部下MA20"},
            "volume": {"score": 48.3, "detail": "两市25419亿"},
            "breadth": {"score": 88.4, "detail": "涨跌4683/725", "up": 4683, "down": 725},
            "zt_emotion": {"score": 84.7, "detail": "涨停99家"},
            "capital": {"score": 88.3, "detail": "主力+638亿"},
        },
        "amount_yi": 25419.0,
        "zt": {"count": 99, "streak_count": 10},
        "top_sectors": [{"name": "文字媒体", "change_pct": 14.99}],
        "bottom_sectors": [{"name": "涂料", "change_pct": -1.3}],
        "holdings": [],
        "plan": ["如果 市场弱势 → 降仓/空仓"],
    }
    md = mr.generate_report(ctx)
    test("含标题", "今日复盘" in md)
    test("含评分", "57.5" in md)
    test("含市场环境", "① 市场环境" in md)
    test("含板块", "② 板块" in md)
    test("含持仓", "③ 持仓" in md)
    test("含明日计划", "④ 明日计划" in md)
    test("含免责声明", "不构成任何投资建议" in md)
    test("含腾讯指数数据源", "腾讯" in md)

    # stale_note 显示
    ctx["stale_note"] = "数据日期 2026-07-31,非今日"
    md = mr.generate_report(ctx)
    test("stale_note 显示", "非今日" in md)

    ctx["portfolio_snapshot"] = {"loaded_at": "2026-08-01 20:00:00", "active_count": 2}
    ctx["holdings_sync_note"] = "持仓已按当前持仓记录刷新"
    md = mr.generate_report(ctx)
    test("持仓快照时间显示", "持仓快照: 2026-08-01 20:00:00" in md)
    test("持仓刷新提示显示", "持仓已按当前持仓记录刷新" in md)


# ──────────────── _index_metrics ────────────────


def test_index_metrics():
    print("\n--- _index_metrics ---")
    # 构建 25 天递增K线
    records = [{"trade_date": f"202607{i+1:02d}", "close": 100 + i,
                "open": 100 + i - 0.5, "high": 100 + i + 1, "low": 100 + i - 1,
                "pct_chg": 1.0, "vol": 1e6, "amount": 1e9} for i in range(25)]
    m = mr._index_metrics(records)
    test("MA20 计算", m["ok"] and m["ma20"] > 0, f"ma20={m['ma20']}")
    test("收盘=最后价", m["close"] == 124.0, f"got {m['close']}")
    test("上升趋势", m["above_ma20"] and m["ma20_rising"])

    # 数据不足 → ok False
    m = mr._index_metrics([{"trade_date": "20260101", "close": 1.0}] * 10)
    test("数据不足", not m.get("ok"))


# ──────────────── 持久化 ────────────────


def test_persistence():
    print("\n--- persistence ---")
    # 用临时缓存目录测 history prune
    old = mr.HISTORY_FILE
    old_max = mr.HISTORY_MAX_DAYS
    with tempfile.TemporaryDirectory() as tmp:
        mr.HISTORY_FILE = Path(tmp) / "market_regime_history.json"
        mr.HISTORY_MAX_DAYS = 3
        for day in (1, 2, 3, 6, 7):
            mr.save_history({"date": f"2026-07-{day:02d}", "regime_score": day})
        hist = mr.load_history()
        test("prune 到最近3个交易日", sorted(hist.keys()) == ["2026-07-03", "2026-07-06", "2026-07-07"],
             f"got {sorted(hist.keys())}")
    mr.HISTORY_FILE = old
    mr.HISTORY_MAX_DAYS = old_max


def test_persistence_rejects_malformed_and_weekend_dates():
    print("\n--- persistence date hygiene ---")
    old = mr.HISTORY_FILE
    with tempfile.TemporaryDirectory() as tmp:
        mr.HISTORY_FILE = Path(tmp) / "history.json"
        test("拒绝紧凑日期", mr.save_history(
            {"date": "20260731", "regime_score": 1}) is False)
        test("拒绝周末日期", mr.save_history(
            {"date": "2026-08-01", "regime_score": 1}) is False)
        test("接受严格交易日", mr.save_history(
            {"date": "2026-08-03", "regime_score": 1}) is True)
        mr.HISTORY_FILE.write_text(json.dumps({
            "20260731": {"date": "20260731"},
            "2026-08-01": {"date": "2026-08-01"},
            "2026-08-03": {"date": "2026-08-03"},
        }), encoding="utf-8")
        hist = mr.load_history()
        test("读取过滤异常日期", sorted(hist) == ["2026-08-03"], str(hist))
    mr.HISTORY_FILE = old


def test_cached_holdings_refresh():
    print("\n--- cached holdings refresh ---")
    old_portfolio = mr.PORTFOLIO_YAML
    with tempfile.TemporaryDirectory() as tmp:
        portfolio_path = Path(tmp) / "portfolio.yaml"
        portfolio_path.write_text(
            "holdings:\n"
            "- code: '601166'\n  name: 兴业银行\n  status: active\n"
            "  stop_loss: 17.0\n  targets: [18.4]\n",
            encoding="utf-8")
        mr.PORTFOLIO_YAML = portfolio_path
        old_meta = mr.portfolio_snapshot_meta(mr.load_portfolio())
        ctx = {
            "regime": {"label": "中性"},
            "holdings": [{"code": "601166", "name": "兴业银行", "ok": True,
                          "close": 18.3, "ma5": 18.4, "ma20": 18.2,
                          "above_ma5": False, "above_ma20": True,
                          "stop_loss": 17.0, "targets": [18.4]}],
            "portfolio_snapshot": old_meta,
        }
        portfolio_path.write_text(
            "holdings:\n"
            "- code: '601166'\n  name: 兴业银行\n  status: closed\n"
            "- code: '588060'\n  name: 科创50ETF\n  status: active\n"
            "  stop_loss: null\n  targets: []\n",
            encoding="utf-8")
        mr.refresh_cached_holdings(ctx)
        codes = [h["code"] for h in ctx["holdings"]]
        test("缓存重出移除已平仓标的", "601166" not in codes, str(codes))
        test("缓存重出加入新增活跃标的", codes == ["588060"], str(codes))
        test("新增持仓标为待实时分析", ctx["holdings"][0]["ok"] is False)
        test("持仓变更给出提示", "持仓已按当前持仓记录刷新" in ctx.get("holdings_sync_note", ""))
        test("计划不再引用已平仓标的", not any("兴业银行" in p for p in ctx["plan"]), str(ctx["plan"]))
    mr.PORTFOLIO_YAML = old_portfolio


# ──────────────── 盘中混合评分 (intraday blend) ────────────────


def test_session_elapsed_fraction():
    print("\n--- _session_elapsed_fraction ---")
    from datetime import datetime
    cases = [
        (datetime(2026, 8, 18, 9, 29), 0.0),
        (datetime(2026, 8, 18, 9, 30), 0.0),
        (datetime(2026, 8, 18, 10, 15), 45 / 240.0),
        (datetime(2026, 8, 18, 11, 30), 0.5),
        (datetime(2026, 8, 18, 12, 0), 0.5),      # 午休
        (datetime(2026, 8, 18, 13, 30), 0.5 + 30 / 240.0),
        (datetime(2026, 8, 18, 15, 0), 1.0),
        (datetime(2026, 8, 18, 15, 1), 0.0),      # 收盘后走全天路径
        (datetime(2026, 8, 22, 10, 0), 0.0),      # 周六
    ]
    for now, want in cases:
        got = mr._session_elapsed_fraction(now)
        test(f"fraction {now.strftime('%m-%d %H:%M')}",
             abs(got - want) < 1e-9, f"got {got} want {want}")


def test_blend_weight():
    print("\n--- _blend_weight ---")
    test("fraction<floor=0", mr._blend_weight(0.10) == 0.0)
    test("fraction=floor=0", mr._blend_weight(0.17) == 0.0)
    test("fraction>=0.75=1", mr._blend_weight(0.75) == 1.0)
    got = mr._blend_weight(0.375)
    test("fraction=0.375≈0.353", abs(got - 0.3534) < 0.01, f"got {got:.4f}")


def test_baseline_history_excludes_partials():
    print("\n--- _baseline_history ---")
    history = {
        "2026-08-14": {"amount_yi": 19327.0, "zt": {"count": 59}},
        "2026-08-17": {"amount_yi": 23875.0, "zt": {"count": 106}},
        "2026-08-18": {"amount_yi": 9914.0, "zt": {"count": 55}},      # 今日 partial
        "2026-08-19": {"amount_yi": 6000.0, "zt": {"count": 30}, "intraday": True},
    }
    base = mr._baseline_history(history, "2026-08-18")
    keys = sorted(base)
    test("排除今日partial", "2026-08-18" not in keys, str(keys))
    test("排除intraday标记", "2026-08-19" not in keys, str(keys))
    test("排除异常低额", "2026-08-19" not in keys)
    test("保留合法全天", "2026-08-14" in keys and "2026-08-17" in keys, str(keys))


def test_baseline_history_excludes_malformed_and_weekend_keys():
    print("\n--- _baseline_history date hygiene ---")
    history = {
        "20260731": {"amount_yi": 1000},
        "2026-08-01": {"amount_yi": 1000},
        "2026-08-03": {"amount_yi": 1000},
    }
    base = mr._baseline_history(history, "2026-08-04")
    test("基线不纳入异常日期", sorted(base) == ["2026-08-03"], str(base))


def test_last_close_context():
    print("\n--- _last_close_context ---")
    history = {
        "2026-08-14": {"regime_score": 56.7, "label": "中性", "amount_yi": 19327.0},
        "2026-08-17": {"regime_score": 83.8, "label": "强势", "amount_yi": 23875.0},
        "2026-08-18": {"regime_score": 46.3, "amount_yi": 9914.0},
    }
    lc = mr._last_close_context(history, "2026-08-18")
    test("取上一交易日", lc and lc["date"] == "2026-08-17" and lc["score"] == 83.8,
         str(lc))
    test("无前收返回None", mr._last_close_context({}, "2026-08-18") is None)
    test("仅今日条目返回None",
         mr._last_close_context({"2026-08-18": {"regime_score": 46.3}}, "2026-08-18") is None)


def test_should_save_history():
    print("\n--- should_save_history ---")
    test("盘中不写历史", mr.should_save_history({"intraday": True}) is False)
    test("全天写历史", mr.should_save_history({"intraday": False}) is True)
    test("缺省写历史", mr.should_save_history({}) is True)


def test_collect_context_intraday_blend_not_weak():
    print("\n--- collect_context 盘中混合不误判弱势 ---")
    from datetime import datetime
    prior_dates = [f"202607{i + 1:02d}" for i in range(1, 21)]
    prior_rows = [
        {"trade_date": d, "close": 3800 + i, "amount": 11000e8, "pct_chg": 0.1}
        for i, d in enumerate(prior_dates)
    ]
    rows_by_code = {
        "000001.SH": prior_rows + [
            {"trade_date": "20260818", "close": 3980.0, "amount": 5000e8, "pct_chg": 0.5},
        ],
        "399106.SZ": prior_rows + [
            {"trade_date": "20260818", "close": 2600.0, "amount": 4914e8, "pct_chg": 0.4},
        ],
        "000300.SH": prior_rows + [
            {"trade_date": "20260818", "close": 4740.0, "amount": 0, "pct_chg": 1.2},
        ],
        "399001.SZ": prior_rows + [
            {"trade_date": "20260818", "close": 10800.0, "amount": 0, "pct_chg": 1.0},
        ],
    }

    def fake_index(code, lmt=80, retries=2, diagnostics=None):
        rows = rows_by_code[code]
        if diagnostics is not None:
            diagnostics.update({"source": "fixture", "record_count": len(rows),
                                "data_date": rows[-1]["trade_date"], "errors": []})
        return rows

    history = {
        "2026-08-17": {
            "regime_score": 83.8, "label": "强势",
            "components": {"index_trend": 100.0, "volume": 80.0, "breadth": 83.3,
                           "zt_emotion": 100.0, "capital": 79.9},
            "amount_yi": 23875.0, "zt": {"count": 106, "streak_count": 15, "max_streak": 4},
        },
        # 已污染 partial 条目(不应成为基线/锚)
        "2026-08-18": {"regime_score": 46.3, "amount_yi": 9914.0,
                       "zt": {"count": 55, "streak_count": 20, "max_streak": 5}},
    }

    def run(now):
        with patch.object(mr, "fetch_index_kline", side_effect=fake_index), \
                patch.object(mr, "fetch_sector_rankings", return_value=[]), \
                patch.object(mr, "fetch_zt_stats",
                             return_value={"count": 55, "streak_count": 20, "max_streak": 5}), \
                patch.object(mr, "fetch_market_activity",
                             return_value={"up": 1827, "down": 3542, "main_force_yi": -232.6}), \
                patch.object(mr, "fetch_northbound", return_value=None), \
                patch.object(mr, "load_history", return_value=history), \
                patch.object(mr, "load_portfolio", return_value=[]):
            return mr.collect_context(now=now)

    # 10:15 — 有外推: 评分不误判弱势(锚昨收强势)
    ctx = run(datetime(2026, 8, 18, 10, 15))
    test("10:15 标记盘中", ctx["intraday"] is True)
    test("10:15 不误判弱势", ctx["regime"]["score"] > 60.0,
         f"score {ctx['regime']['score']} label {ctx['regime']['label']}")
    test("10:15 成交额外推为全天额", (ctx["amount_yi"] or 0) > 20000,
         f"amount {ctx['amount_yi']}")
    test("10:15 含盘中说明", "盘中" in ctx.get("intraday_note", ""), ctx.get("intraday_note", ""))

    # 9:40 — fraction<FLOOR: 纯昨收锚(≈83.8)
    ctx_early = run(datetime(2026, 8, 18, 9, 40))
    test("9:40 标记盘中", ctx_early["intraday"] is True)
    test("9:40 纯昨收锚", abs(ctx_early["regime"]["score"] - 83.8) < 1.0,
         f"score {ctx_early['regime']['score']}")

    # 收盘后(15:30) — 非盘中,走全天路径
    ctx_close = run(datetime(2026, 8, 18, 15, 30))
    test("收盘后非盘中", ctx_close["intraday"] is False)
    test("收盘后无盘中说明", ctx_close.get("intraday_note", "") == "")


# ──────────────── live loaders (guarded) ────────────────


def test_live_loaders():
    print("\n--- live loaders ---")
    if not mr.HAS_AKSHARE:
        skip("zt 直连(AKShare 未安装)")
    else:
        zt = mr.fetch_zt_stats()
        test("zt 结构", isinstance(zt, dict) and "count" in zt and "max_streak" in zt)
    try:
        import requests  # noqa: F401
        has_requests = True
    except ImportError:
        has_requests = False
    if not has_requests:
        skip("fetch_market_activity(无 requests)")
    else:
        act = mr.fetch_market_activity()
        test("market_activity 结构", act is None or ("up" in act and "down" in act))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    test_index_trend()
    test_volume()
    test_index_fallback_and_amount()
    test_collect_context_rejects_stale_turnover_leg()
    test_breadth()
    test_zt()
    test_capital()
    test_regime()
    test_plan()
    test_report()
    test_index_metrics()
    test_persistence()
    test_persistence_rejects_malformed_and_weekend_dates()
    test_cached_holdings_refresh()
    test_session_elapsed_fraction()
    test_blend_weight()
    test_baseline_history_excludes_partials()
    test_baseline_history_excludes_malformed_and_weekend_keys()
    test_last_close_context()
    test_should_save_history()
    test_collect_context_intraday_blend_not_weak()
    test_live_loaders()

    print(f"\nResults: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    if args.verbose:
        for r in RESULTS:
            if r["status"] == "FAIL":
                print(f"  FAIL {r['name']}: {r['detail']}")
    sys.exit(0 if FAILED == 0 else 1)


if __name__ == "__main__":
    main()
