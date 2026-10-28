#!/usr/bin/env python3
"""Reproduce the frozen six-interval weak-to-strong v2 research validation.

The fixture contains historical OHLCV and a trading calendar only.  Every
close confirmation used here is explicitly marked ``simulated_for_research``;
the output is retrospective technical validation, not archived publication or
formal close evidence.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path


SCRIPT = Path(__file__).resolve()
SKILL_ROOT = SCRIPT.parents[2]
REPO_ROOT = SCRIPT.parents[5]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from analysis.daily_weak_to_strong_v2 import (  # noqa: E402
    DEFAULT_CONFIG, DETECTOR_VERSION, analyze_daily_weak_to_strong_v2,
)


CASES = (
    ("ocean", "海洋王", "002724.SZ", "2026-08-20", "2026-10-13"),
    ("xiangyang_1", "襄阳轴承", "000678.SZ", "2026-08-25", "2026-09-24"),
    ("xiangyang_2", "襄阳轴承", "000678.SZ", "2026-09-24", "2026-10-22"),
    ("wanxiang", "万向钱潮", "000559.SZ", "2026-10-13", "2026-10-16"),
    ("shenzhen", "深科技", "000021.SZ", "2026-05-06", "2026-06-16"),
    ("lead", "先导基电", "600641.SH", "2026-04-15", "2026-06-10"),
)
KEY_EXPECTATIONS = (
    ("002724.SZ", "2026-09-16", "EARLY_REPAIR", None),
    ("002724.SZ", "2026-09-21", "RANGE_BREAKOUT", None),
    ("002724.SZ", "2026-10-13", "EARLY_REPAIR", None),
    ("000678.SZ", "2026-09-24", "RANGE_BREAKOUT", None),
    ("000678.SZ", "2026-10-16", "EARLY_REPAIR", None),
    ("000678.SZ", "2026-10-22", "LOCAL_CONFIRMATION", 13.90),
    ("000021.SZ", "2026-06-16", "LOCAL_CONFIRMATION", 46.81),
    # The causal anchor is frozen from T-1 when the structure is created on
    # 5/15.  That yields 31.60; the 5/15 intraday high 31.80 remains a later
    # observed overhead price and is not backfilled into the frozen anchor.
    ("600641.SH", "2026-06-10", "EARLY_REPAIR", 31.60),
    ("000559.SZ", "2026-10-16", "PREPARING", None),
)
STAGE_RANK = {"NONE": 0, "PREPARING": 1, "EARLY_REPAIR": 2,
              "LOCAL_CONFIRMATION": 3, "RANGE_BREAKOUT": 4}
SIMULATED_CLOSE = {
    "confirmed": True,
    "source": "simulated_for_research",
    "research_only": True,
    "formal_close_evidence": False,
}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _date(row):
    text = str(row.get("trade_date") or row.get("date") or "")
    digits = "".join(character for character in text if character.isdigit())
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}" if len(digits) == 8 else ""


def freeze_fixture(source_dir, destination):
    """Create a compact, source-hash-bound fixture from the authorized report."""
    source_dir, destination = Path(source_dir), Path(destination)
    sources = {}
    earliest, latest = "9999-12-31", ""
    for code in sorted({case[2] for case in CASES}):
        path = source_dir / f"{code}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = [row for row in raw.get("data", []) if isinstance(row, dict)]
        dates = [_date(row) for row in rows]
        earliest, latest = min(earliest, min(dates)), max(latest, max(dates))
        meta = raw.get("meta") or {}
        sources[code] = {
            "source_sha256": _sha(path),
            "payload": {
                "meta": {
                    "ts_code": code, "asset": meta.get("asset", "E"),
                    "freq": meta.get("freq", "D"), "adj": meta.get("adj"),
                    "record_count": len(rows), "data_source": meta.get("data_source"),
                    "fetch_time": meta.get("fetch_time"),
                    "volume_unit": meta.get("volume_unit", "unknown"),
                    "fixture_source_sha256": _sha(path),
                },
                "data": rows,
            },
        }
    calendar_path = source_dir / "calendar.json"
    calendar_raw = json.loads(calendar_path.read_text(encoding="utf-8"))
    dates = [day for day in calendar_raw.get("trading_dates", [])
             if earliest <= day <= latest]
    fixture = {
        "schema_version": "weak-to-strong-v2-research-fixture/v1",
        "research_only": True,
        "formal_close_evidence": False,
        "close_evidence_used_by_validation": "simulated_for_research",
        "source_report": "reports/research/weak-to-strong-20261028",
        "sources": sources,
        "calendar": {
            "source_sha256": _sha(calendar_path),
            "evidence": {
                "source": "frozen_research_fixture", "identity": _sha(calendar_path),
                "coverage_start": dates[0], "coverage_end": dates[-1],
                "checked_date": calendar_raw.get("checked_date"),
                "trading_dates": dates,
            },
        },
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return fixture


def load_fixture(path):
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if fixture.get("schema_version") != "weak-to-strong-v2-research-fixture/v1":
        raise ValueError("fixture_schema_invalid")
    if fixture.get("formal_close_evidence") is not False:
        raise ValueError("fixture_close_evidence_misrepresented")
    return fixture


def _analyze(fixture, code, name, day, payload=None):
    payload = payload or fixture["sources"][code]["payload"]
    return analyze_daily_weak_to_strong_v2(
        payload, as_of_date=day, code=code, name=name,
        close_evidence=SIMULATED_CLOSE,
        calendar_evidence=fixture["calendar"]["evidence"],
    )


def _canonical_result(result):
    value = copy.deepcopy(result)
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _truncated(payload, day):
    value = copy.deepcopy(payload)
    value["data"] = [row for row in value.get("data", []) if _date(row) <= day]
    return value


def _with_future_extreme(payload):
    value = copy.deepcopy(payload)
    value["data"].append({
        "trade_date": "20991231", "open": 1.0, "close": 9999.0,
        "high": 10000.0, "low": 0.01, "vol": 999999999999.0,
        "amount": 0, "ts_code": "FUTURE.IGNORED",
    })
    return value


def _interval_days(payload, start, end):
    return [_date(row) for row in payload.get("data", []) if start <= _date(row) <= end]


def _failed_initial_checks(result):
    checks = result.get("checks") or {}
    return sorted(key for key, value in checks.items()
                  if isinstance(value, dict) and value.get("passed") is False)


def validate_fixture(fixture, performance_runs=3):
    interval_results, prefix_checks, future_checks = [], 0, 0
    unique_days = set()
    for case_id, name, code, start, end in CASES:
        payload = fixture["sources"][code]["payload"]
        days = _interval_days(payload, start, end)
        rows = []
        for day in days:
            full = _analyze(fixture, code, name, day, payload)
            prefix = _analyze(fixture, code, name, day, _truncated(payload, day))
            future = _analyze(fixture, code, name, day, _with_future_extreme(payload))
            if _canonical_result(full) != _canonical_result(prefix):
                raise AssertionError(f"prefix_invariance_failed:{case_id}:{day}")
            if _canonical_result(full) != _canonical_result(future):
                raise AssertionError(f"future_invariance_failed:{case_id}:{day}")
            prefix_checks += 1
            future_checks += 1
            unique_days.add((code, day))
            rows.append({
                "date": day, "stage": full.get("stage"),
                "pattern_type": full.get("pattern_type"),
                "evidence_status": full.get("evidence_status"),
                "signal_date": full.get("signal_date"),
                "failed_initial_checks": _failed_initial_checks(full),
            })
        interval_results.append({"case_id": case_id, "name": name, "code": code,
                                 "start": start, "end": end, "trading_days": len(days),
                                 "rows": rows})

    key_results = []
    for code, day, expected_stage, expected_parent in KEY_EXPECTATIONS:
        name = next(case[1] for case in CASES if case[2] == code)
        result = _analyze(fixture, code, name, day)
        if result.get("stage") != expected_stage:
            raise AssertionError(
                f"key_stage_failed:{code}:{day}:{result.get('stage')}:{expected_stage}")
        actual_parent = (result.get("structure") or {}).get("anchor_high")
        if expected_parent is not None and (
                actual_parent is None or abs(actual_parent - expected_parent) > 1e-9):
            raise AssertionError(
                f"parent_high_failed:{code}:{day}:{actual_parent}:{expected_parent}")
        key_results.append({
            "code": code, "date": day, "stage": result.get("stage"),
            "pattern_type": result.get("pattern_type"), "parent_high": actual_parent,
            "failed_initial_checks": _failed_initial_checks(result),
        })

    wanxiang = _analyze(fixture, "000559.SZ", "万向钱潮", "2026-10-16")
    metrics = wanxiang.get("metrics") or {}
    wanxiang_checks = {
        "stage_not_above_preparing": STAGE_RANK.get(wanxiang.get("stage"), 0) <= 1,
        "below_ma10": metrics.get("close") < metrics.get("ma10"),
        "below_ma20": metrics.get("close") < metrics.get("ma20"),
        "close_strength_below_0_60": metrics.get("close_strength") < 0.60,
        "below_h5": metrics.get("close") < metrics.get("h5"),
    }
    if not all(wanxiang_checks.values()):
        raise AssertionError("wanxiang_negative_control_failed")

    batch_seconds = []
    latest = max(_date(row) for source in fixture["sources"].values()
                 for row in source["payload"]["data"])
    for _ in range(performance_runs):
        started = time.perf_counter()
        for code, source in sorted(fixture["sources"].items()):
            name = next(case[1] for case in CASES if case[2] == code)
            _analyze(fixture, code, name, latest, source["payload"])
        batch_seconds.append(time.perf_counter() - started)
    performance = {
        "scope": "frozen_five_security_batch_not_full_market",
        "runs": performance_runs, "securities_per_run": 5,
        "median_seconds": statistics.median(batch_seconds) if batch_seconds else None,
        "max_seconds": max(batch_seconds) if batch_seconds else None,
        "samples_seconds": batch_seconds,
        "network_requests": 0,
    }
    return {
        "schema_version": "weak-to-strong-v2-validation/v1",
        "research_only": True, "formal_close_evidence": False,
        "close_evidence": copy.deepcopy(SIMULATED_CLOSE),
        "detector_version": DETECTOR_VERSION,
        "parameters": copy.deepcopy(DEFAULT_CONFIG),
        "fixture_identity": {
            "sources": {code: source["source_sha256"]
                        for code, source in fixture["sources"].items()},
            "calendar": fixture["calendar"]["source_sha256"],
        },
        "counts": {"interval_rows": prefix_checks,
                   "unique_security_dates": len(unique_days),
                   "prefix_invariance_passed": prefix_checks,
                   "future_append_invariance_passed": future_checks},
        "intervals": interval_results, "key_expectations": key_results,
        "causal_anchor_note": (
            "600641.SH freezes anchor_high=31.60 from T-1 at the 2026-05-15 "
            "structure creation; the same-day high 31.80 is not backfilled."
        ),
        "negative_control": {"code": "000559.SZ", "date": "2026-10-16",
                             "checks": wanxiang_checks, "metrics": metrics},
        "performance": performance,
        "validation_status": "continue_accumulating",
    }


def render_markdown(report):
    counts, perf = report["counts"], report["performance"]
    lines = [
        "# 日线弱转强 v2 冻结样本验证", "",
        "本报告使用冻结历史行情作回顾性技术验证。收盘证据标记为 "
        "`simulated_for_research`，不代表当日已归档的正式收盘确认或历史推荐。", "",
        f"- 检测器：`{report['detector_version']}`",
        f"- 验证状态：`{report['validation_status']}`",
        f"- 六段逐日记录：{counts['interval_rows']}；证券/日期去重：{counts['unique_security_dates']}",
        f"- 逐日前缀一致性：{counts['prefix_invariance_passed']} / {counts['interval_rows']}",
        f"- 追加未来极端行情不变性：{counts['future_append_invariance_passed']} / {counts['interval_rows']}",
        "", "## 关键日期", "", "|代码|日期|阶段|背景|父结构高点|", "|---|---|---|---|---|",
    ]
    for item in report["key_expectations"]:
        lines.append(f"|{item['code']}|{item['date']}|{item['stage']}|"
                     f"{item['pattern_type']}|{item['parent_high'] or '—'}|")
    negative = report["negative_control"]
    lines += ["", "## 万向钱潮反例", "",
              "2026-10-16 仅为准备观察；仍低于 MA10、MA20 和 H5，收盘强度低于 0.60。"
              "单日上涨不能取得转强资格。", "", "## 因果锚点说明", "",
              report["causal_anchor_note"], "", "## 性能（冻结五股批量，非全市场）", "",
              f"- 运行 {perf['runs']} 次，每次 5 只；中位数 {perf['median_seconds']} 秒，"
              f"最大 {perf['max_seconds']} 秒。", "- 网络请求：0。", "",
              "本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    default_fixture = (SKILL_ROOT / "tests" / "fixtures" /
                       "weak_to_strong_v2" / "frozen_inputs.json")
    parser.add_argument("--fixture", type=Path, default=default_fixture)
    parser.add_argument("--freeze-source", type=Path)
    parser.add_argument("--output-dir", type=Path,
                        default=REPO_ROOT / "reports" / "research" /
                        "weak-to-strong-v2-20261028")
    parser.add_argument("--performance-runs", type=int, default=3)
    args = parser.parse_args(argv)
    if args.freeze_source:
        freeze_fixture(args.freeze_source, args.fixture)
    fixture = load_fixture(args.fixture)
    report = validate_fixture(fixture, performance_runs=args.performance_runs)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "validation.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": "ok", "output_dir": str(args.output_dir),
                      "counts": report["counts"], "performance": report["performance"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
