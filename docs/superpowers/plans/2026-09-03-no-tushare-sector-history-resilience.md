# No-Tushare Sector History Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在没有 Tushare 权限和新增付费数据源的前提下，可靠地按交易日积累候选板块完整快照，并在数据不完整时保持观察态而不伪造持续性。

**Architecture:** 保留东方财富 `push2` 全量板块排行作为唯一可写入 `candidate_sector_history.json` 的免费主口径，复用现有多 Host 轮换与 AKShare 降级；新增轻量的收盘后快照采集命令，使历史积累不再依赖完整候选股扫描。AKShare/同花顺行业数据和 BK 历史 K 线仅作为旁证，不能提升 `history_coverage_days`；将来获得 JQData、RQData、Wind 或 iFinD 权限时，再通过独立适配器接入，不纳入本期 MVP。

**Tech Stack:** Python 3.10+、现有 `urllib`/AKShare、JSON 原子写入、`unittest`/pytest 兼容测试、现有 `RunSourceHealth` 与板块评分函数。

---

## Requirements Summary

1. 不使用 Tushare SDK、Token 或任何 Tushare 接口。
2. 不引入新的付费服务或 Python 依赖。
3. 不根据旧 Top-30 缺席、当前成分反推历史成分，也不把 BK K线冒充完整板块截面。
4. 每个有效交易日收盘后，只要东方财富行业和概念两类排行都完整，就保存候选口径完整排行。
5. 快照采集应独立于耗时的个股候选扫描，可单独执行并返回机器可读状态。
6. 同一天重复采集幂等；日期不匹配、盘中、休市、稀疏或部分排行不得增加覆盖天数。
7. 现有 `/candidates` 行为、Top-30 快照语义和 golden 输出保持兼容。
8. 当前 `1/10` 状态在下一个成功收盘快照后应达到至少 `2/10`；这只消除“历史不足”，不保证板块晋级为 `emerging` 或 `mainline`。

## Source Policy

| 层级 | 数据源 | 用途 | 可否计入完整覆盖 |
| --- | --- | --- | --- |
| A | 东方财富 `push2` 行业+概念完整截面，多 Host 轮换 | 当前排行、正式候选快照 | 是 |
| B | AKShare 同花顺行业摘要 | 故障旁证、诊断 | 否；概念没有同口径活动字段 |
| C | 东方财富 BK 历史 K线 | 验证价格方向和相对强弱 | 否；缺少完整历史资金和涨跌家数截面 |
| D | 最近一次本地完整排行缓存 | 非交易日展示和故障降级 | 否；不能创建新日期 |
| Deferred | JQData/RQData/Wind/iFinD | 真正独立供应商备份 | 获得权限后另立计划 |

## File Map

- Create: `.claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py` — 收盘后板块快照采集 CLI，只负责日期校验、拉取、评分、持久化和状态输出。
- Modify: `.claude/skills/stock-trend/scripts/fetchers/sector_data.py:1030-1165` — 提取可复用的“完整候选快照提交”函数，保持现有 schema 和原子写入。
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1037-1185` — 改为复用快照提交函数，避免 CLI 与候选扫描产生两套写入规则。
- Create: `.claude/skills/stock-trend/tests/test_sector_snapshot_job.py` — 独立采集命令的日期、完整性、幂等和失败行为测试。
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:472-610,1270-1332` — 锁定提取后兼容行为。
- Modify: `.claude/skills/stock-trend/SKILL.md:276-295` — 在 `/candidates` 说明中加入收盘快照采集命令和数据口径。
- Modify: `docs/usage-guide.md` — 增加无 Tushare 的日常采集、故障诊断和恢复说明。

## Acceptance Criteria

1. 完整东方财富截面包含行业、概念两类，且每类至少 5 条有效记录时，采集命令退出码为 `0`，输出 `status=saved`，候选快照新增准确交易日。
2. 行业或概念任一来源为空、稀疏或报错时，退出码为非零，输出 `status=incomplete`，快照文件字节内容不变。
3. 盘中默认执行返回 `status=not_closed`；休市日返回 `status=market_closed`；两者均不写历史。
4. `--date YYYY-MM-DD` 只能显式指定当天交易日，不能用来伪造过去日期；若排行声明的 `data_date` 与参数不一致，必须失败且不写入。
5. 同一交易日重复成功执行仅替换同一日期记录，历史日期数量不增加。
6. 正式快照保持 `complete=true`、`quality=good`、`source=eastmoney`，并保存 `universe_count`、`ranked_count`、过滤参数和全部候选口径排行。
7. AKShare 返回的行业-only 或概念零活动结果不能写入正式候选快照，也不能令 `history_coverage_days` 增加。
8. 一个旧完整快照加一个新完整快照时，报告不再显示 `history_insufficient`；若只有最新一天达到热点门槛，仍为 `single_day_pulse` 且 `sector_actionable=false`。
9. 原 Top-30 `sector_snapshot_history.json` 的结构、消费者和 golden 输出无变化。
10. 两个仓库强制质量门全部通过：`test_stock_trend.py` 与 `test_golden.py --diff`。

### Task 1: Lock the reusable snapshot-commit contract

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:472-610`
- Modify: `.claude/skills/stock-trend/scripts/fetchers/sector_data.py:1030-1165`

- [ ] **Step 1: Add failing tests for a single commit boundary**

在 `test_daily_candidates.py` 的候选快照测试组增加：

```python
def test_commit_candidate_snapshot_accepts_only_complete_eastmoney_rankings(self):
    from fetchers import sector_data

    rankings = {
        "meta": {
            "complete": True,
            "source": "eastmoney",
            "data_date": "2026-09-03",
            "total_sectors": 12,
            "sources": {"industry": "ok", "concept": "ok"},
        },
        "sectors": [
            {
                "code": f"BK{i:04d}", "name": f"板块{i}",
                "change_pct": 1.0, "main_force_net": 1e8,
                "up_count": 9, "down_count": 1,
            }
            for i in range(12)
        ],
    }
    with tempfile.TemporaryDirectory() as tmpdir, patch.object(
        sector_data, "CANDIDATE_SNAPSHOT_FILE",
        Path(tmpdir) / "candidate-history.json",
    ):
        result = sector_data.commit_candidate_sector_snapshot(
            rankings, data_date="2026-09-03", min_stocks=1,
        )
        history = sector_data.load_candidate_sector_history(days=10)

    self.assertEqual(result["status"], "saved")
    self.assertEqual(result["ranked_count"], 12)
    self.assertEqual(len(history["2026-09-03"]["sectors"]), 12)


def test_commit_candidate_snapshot_rejects_partial_provider_without_writing(self):
    from fetchers import sector_data

    rankings = {
        "meta": {
            "complete": False,
            "source": "akshare",
            "data_date": "2026-09-03",
            "sources": {"industry": "ok", "concept": "empty"},
        },
        "sectors": [{
            "code": "881121", "name": "半导体", "change_pct": 1.0,
            "main_force_net": 1e8, "up_count": 9, "down_count": 1,
        }],
    }
    with tempfile.TemporaryDirectory() as tmpdir, patch.object(
        sector_data, "CANDIDATE_SNAPSHOT_FILE",
        Path(tmpdir) / "candidate-history.json",
    ):
        result = sector_data.commit_candidate_sector_snapshot(
            rankings, data_date="2026-09-03", min_stocks=1,
        )
        exists = sector_data.CANDIDATE_SNAPSHOT_FILE.exists()

    self.assertEqual(result["status"], "incomplete")
    self.assertFalse(exists)
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run:

```bash
python3 -m pytest .claude/skills/stock-trend/tests/test_daily_candidates.py -k 'commit_candidate_snapshot' -q
```

Expected: FAIL because `commit_candidate_sector_snapshot` does not exist.

- [ ] **Step 3: Implement the minimal reusable commit function**

在 `append_candidate_sector_snapshot()` 之前增加：

```python
def commit_candidate_sector_snapshot(
        rankings: dict, data_date: str, min_stocks: int = 10,
        min_up_ratio: float = 0.15) -> dict:
    """Rank and persist one complete BK candidate-sector snapshot."""
    meta = rankings.get("meta", {}) if isinstance(rankings, dict) else {}
    sectors = rankings.get("sectors", []) if isinstance(rankings, dict) else []
    if meta.get("complete") is not True or not sectors:
        return {"status": "incomplete", "ranked_count": 0}
    if meta.get("source", "eastmoney") not in ("eastmoney", "realtime"):
        return {"status": "unsupported_source", "ranked_count": 0}
    if meta.get("sources") and not all(
            meta["sources"].get(name) == "ok"
            for name in ("industry", "concept")):
        return {"status": "incomplete", "ranked_count": 0}

    meta.setdefault("source", "eastmoney")

    ranked = rank_hot_sectors(
        rankings, top_n=None,
        min_stocks=min_stocks, min_up_ratio=min_up_ratio,
    )
    if not ranked:
        return {"status": "incomplete", "ranked_count": 0}
    append_candidate_sector_snapshot(
        rankings,
        ranked=ranked,
        override_date=data_date,
        filter_meta={
            "min_stocks": min_stocks,
            "min_up_ratio": min_up_ratio,
            "deduplicated": True,
        },
    )
    return {
        "status": "saved",
        "data_date": data_date,
        "universe_count": len(sectors),
        "ranked_count": len(ranked),
    }
```

实现时保留 `append_candidate_sector_snapshot()` 作为底层 schema/原子写入函数，不复制 JSON 写入逻辑。

- [ ] **Step 4: Run the focused tests**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit the contract extraction**

```bash
git add .claude/skills/stock-trend/scripts/fetchers/sector_data.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "refactor: centralize sector snapshot commit"
```

### Task 2: Make `/candidates` use the shared commit boundary

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1037-1185`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:1270-1332`

- [ ] **Step 1: Update the existing mock-based test to expect the shared function**

将 `test_pick_hot_sectors_writes_full_candidate_snapshot_and_uses_current_data` 对 `append_candidate_sector_snapshot` 的 patch 改为：

```python
patch("fetchers.sector_data.commit_candidate_sector_snapshot") as commit_snapshot
```

并断言：

```python
commit_snapshot.assert_called_once_with(
    rankings,
    data_date="2026-08-06",
    min_stocks=1,
    min_up_ratio=0.15,
)
```

保留当前快照参与本次持续性计算的断言，因为磁盘写入失败也不应抹掉当日内存证据。

- [ ] **Step 2: Run the focused integration tests and confirm failure**

```bash
python3 -m pytest .claude/skills/stock-trend/tests/test_daily_candidates.py -k 'pick_hot_sectors_writes_full_candidate_snapshot or candidate_snapshot_write_failure' -q
```

Expected: FAIL because production code still calls the lower-level append function.

- [ ] **Step 3: Replace the duplicated ranking/write block**

在 `pick_hot_sectors()` 的 import 列表中用 `commit_candidate_sector_snapshot` 替换 `append_candidate_sector_snapshot`，并将 1169-1185 附近的写入改为：

```python
if active and live_meta.get("complete", False) and as_of_date:
    try:
        commit_candidate_sector_snapshot(
            rankings,
            data_date=as_of_date,
            min_stocks=min_stocks,
            min_up_ratio=0.15,
        )
    except (OSError, TypeError, ValueError) as exc:
        _record_degradation(
            metrics,
            f"candidate_sector_snapshot_write_error:{type(exc).__name__}",
        )
```

不得改变 `ranked_universe`、`qualified` 或 `current_snapshot` 的现有计算顺序。

- [ ] **Step 4: Run candidate tests**

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit the caller migration**

```bash
git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "refactor: reuse sector snapshot commit"
```

### Task 3: Add a standalone post-close snapshot job

**Files:**
- Create: `.claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py`
- Create: `.claude/skills/stock-trend/tests/test_sector_snapshot_job.py`

- [ ] **Step 1: Write failing job tests**

测试文件至少覆盖以下纯函数入口：

```python
def test_capture_saves_complete_post_close_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(job, "get_sector_rankings", lambda **_: complete_rankings())
    monkeypatch.setattr(job, "commit_candidate_sector_snapshot", lambda *_, **__: {
        "status": "saved", "data_date": "2026-09-03", "ranked_count": 715,
    })
    result = job.capture_snapshot(
        now=datetime(2026, 9, 3, 15, 20), expected_date="2026-09-03"
    )
    assert result["status"] == "saved"


def test_capture_refuses_intraday_write(monkeypatch):
    called = False

    def fail_fetch(**_):
        nonlocal called
        called = True

    monkeypatch.setattr(job, "get_sector_rankings", fail_fetch)
    result = job.capture_snapshot(
        now=datetime(2026, 9, 3, 14, 59), expected_date="2026-09-03"
    )
    assert result["status"] == "not_closed"
    assert called is False


def test_capture_does_not_save_partial_rankings(monkeypatch):
    monkeypatch.setattr(job, "get_sector_rankings", lambda **_: {
        "meta": {"complete": False, "errors": ["concept: empty"]},
        "sectors": [],
    })
    result = job.capture_snapshot(
        now=datetime(2026, 9, 3, 15, 20), expected_date="2026-09-03"
    )
    assert result["status"] == "incomplete"
```

同时增加休市日、日期不一致、同日幂等、JSON 输出不包含原始异常堆栈的测试。

- [ ] **Step 2: Run tests and confirm failure**

```bash
python3 -m pytest .claude/skills/stock-trend/tests/test_sector_snapshot_job.py -q
```

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the job with no report or stock scan side effects**

核心接口：

```python
def capture_snapshot(now=None, expected_date="", dry_run=False) -> dict:
    current = now or datetime.now()
    if current.weekday() >= 5:
        return {"status": "market_closed", "written": False}
    if current.hour * 60 + current.minute < 15 * 60 + 10:
        return {"status": "not_closed", "written": False}

    trading_date, date_source = get_last_trading_day(now=current)
    data_date = expected_date or trading_date
    if not data_date or data_date != current.strftime("%Y-%m-%d"):
        return {
            "status": "market_closed", "written": False,
            "date_source": date_source,
        }

    rankings = get_sector_rankings(with_evidence=True)
    payload = rankings["payload"]
    meta = payload.get("meta", {})
    if meta.get("complete") is not True:
        return {
            "status": "incomplete", "written": False,
            "data_date": data_date,
            "errors": list(meta.get("errors", [])),
        }
    payload.setdefault("meta", {})["data_date"] = data_date
    payload["meta"].setdefault("source", "eastmoney")
    if dry_run:
        return {
            "status": "validated", "written": False,
            "data_date": data_date,
            "universe_count": len(payload.get("sectors", [])),
        }

    save_rankings_cache(payload, data_date=data_date)
    append_daily_snapshot(payload, override_date=data_date)
    result = commit_candidate_sector_snapshot(payload, data_date=data_date)
    return {**result, "written": result.get("status") == "saved"}
```

CLI 参数限定为：

```text
--date YYYY-MM-DD   显式指定当天日期；不能创建过去日期
--dry-run           拉取并验证，但不写缓存或历史
--json              标准输出单个 JSON 对象
```

主函数按状态返回退出码：`saved/validated=0`，`not_closed/market_closed=2`，`incomplete/error=1`。不得生成候选报告、推荐快照或打开浏览器。

- [ ] **Step 4: Run job tests**

```bash
python3 -m pytest .claude/skills/stock-trend/tests/test_sector_snapshot_job.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the standalone job**

```bash
git add .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py .claude/skills/stock-trend/tests/test_sector_snapshot_job.py
git commit -m "feat: add post-close sector snapshot job"
```

### Task 4: Expose honest diagnostics instead of pretending to backfill

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py`
- Modify: `.claude/skills/stock-trend/tests/test_sector_snapshot_job.py`
- Modify: `docs/usage-guide.md`

- [ ] **Step 1: Add a failing status test**

```python
def test_status_reports_coverage_and_next_requirement(monkeypatch):
    monkeypatch.setattr(job, "load_candidate_sector_history", lambda days: {
        "2026-09-02": {"complete": True, "quality": "good", "sectors": [{}]},
    })
    result = job.snapshot_status(as_of_date="2026-09-03", days=10)
    assert result == {
        "as_of_date": "2026-09-03",
        "coverage_days": 1,
        "minimum_days": 2,
        "days_needed": 1,
        "classification_ready": False,
    }
```

- [ ] **Step 2: Implement `--status` as a read-only mode**

```python
def snapshot_status(as_of_date: str, days: int = 10) -> dict:
    history = load_candidate_sector_history(days=days)
    coverage = sum(
        isinstance(record, dict)
        and record.get("complete") is True
        and record.get("quality") == "good"
        and bool(record.get("sectors"))
        for date, record in history.items()
        if date <= as_of_date
    )
    return {
        "as_of_date": as_of_date,
        "coverage_days": coverage,
        "minimum_days": 2,
        "days_needed": max(0, 2 - coverage),
        "classification_ready": coverage >= 2,
    }
```

`--status` 不联网、不写文件，明确区分“完整截面覆盖”和“已有 Top-30 正向证据”。

- [ ] **Step 3: Document the no-Tushare operating path**

在 `docs/usage-guide.md` 增加：

```bash
# 先检查本地覆盖，不联网、不写入
python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --status --json

# 每个交易日 15:10 后采集完整板块截面
python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --json

# 仅验证实时源完整性
python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --dry-run --json
```

说明：失败日保持缺口，不用当前成分或 BK K线伪造；下一交易日继续采集。建议在实际执行阶段由既有任务调度器调用，不在本计划中自动安装 `launchd`/cron，避免修改用户级系统配置。

- [ ] **Step 4: Run status tests and commit**

```bash
python3 -m pytest .claude/skills/stock-trend/tests/test_sector_snapshot_job.py -q
git add .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py .claude/skills/stock-trend/tests/test_sector_snapshot_job.py docs/usage-guide.md
git commit -m "docs: add no-tushare snapshot operations"
```

### Task 5: Update skill guidance and run full verification

**Files:**
- Modify: `.claude/skills/stock-trend/SKILL.md:276-295`
- Verify: all changed Python and documentation files

- [ ] **Step 1: Add the operational contract to `/candidates` guidance**

增加以下规则：完整持续性快照只接受收盘后的东方财富 BK 全量截面；AKShare 行业数据、历史 BK K线和旧 Top-30 只提供旁证。记录标准采集命令，并说明首次上线通常需要 2–3 个交易日积累。

- [ ] **Step 2: Run syntax and focused tests**

```bash
python3 -m py_compile \
  .claude/skills/stock-trend/scripts/fetchers/sector_data.py \
  .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py \
  .claude/skills/stock-trend/scripts/scans/daily_candidates.py
python3 -m pytest \
  .claude/skills/stock-trend/tests/test_sector_snapshot_job.py \
  .claude/skills/stock-trend/tests/test_daily_candidates.py -q
```

Expected: compilation succeeds and all focused tests pass.

- [ ] **Step 3: Run the two mandatory repository quality gates**

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: both commands pass; golden files remain unchanged.

- [ ] **Step 4: Run non-writing CLI smoke checks**

```bash
python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --status --json
python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --dry-run --json
```

Expected: each prints one valid JSON object; `--status` performs no network access, `--dry-run` performs no writes. If live network is unavailable, `--dry-run` may return `status=incomplete/error` but must not modify cache history.

- [ ] **Step 5: Confirm no unintended file changes**

```bash
git diff --check
git status --short
git diff -- \
  .claude/skills/stock-trend/scripts/fetchers/sector_data.py \
  .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py \
  .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
  .claude/skills/stock-trend/tests/test_sector_snapshot_job.py \
  .claude/skills/stock-trend/tests/test_daily_candidates.py \
  .claude/skills/stock-trend/SKILL.md \
  docs/usage-guide.md
```

Expected: only planned files differ; no cache, report, golden, credential, proxy or user-level scheduler files are changed.

- [ ] **Step 6: Commit final guidance**

```bash
git add .claude/skills/stock-trend/SKILL.md docs/usage-guide.md
git commit -m "docs: define free sector history policy"
```

## Risks and Mitigations

- **东方财富整体不可用：** 多 Host 只能解决节点故障。当天保持缺口并输出低基数原因码，绝不使用旧缓存创建新日期。
- **AKShare看似成功但概念字段为零：** 继续要求行业、概念均 `ok` 且存在活动记录；AKShare只作诊断旁证。
- **盘中数据被当作收盘数据：** 默认 15:10 前拒绝写入；显式日期也必须与交易日历和上游日期一致。
- **代码重构改变候选排序：** 提取提交函数但不改变 `ranked_universe` 的内存计算；用现有排序和持续性测试锁定行为。
- **连续运行覆盖较好的同日数据：** 仅允许同日期完整快照覆盖完整快照；部分结果绝不进入底层 append。
- **用户期待立即补满 10 天：** 文档明确无可靠免费历史截面时不能安全回填；最低判定只需再成功采集 1 天，完整 10 日窗口自然积累。
- **同源风险：** 东方财富直连、备用 Host 和 AKShare 包装不是独立供应商；未来若需要供应商级容灾，另行评估 JQData/RQData/Wind/iFinD 授权和板块映射准确率。

## Verification Matrix

| Claim | Proof |
| --- | --- |
| 完整截面可写 | `test_capture_saves_complete_post_close_snapshot` |
| 部分截面不污染历史 | `test_capture_does_not_save_partial_rankings` + 文件字节不变断言 |
| 盘中/休市不写 | `test_capture_refuses_intraday_write` + 休市测试 |
| 同日幂等 | 两次采集后日期数量仍为 1 |
| AKShare不提升覆盖 | unsupported/partial provider test + persistence test |
| 2天后不再历史不足 | existing two-day candidate persistence test |
| Top-30消费者不变 | `test_market_theme.py`、`test_weekly_report.py`、golden diff |
| 项目质量门通过 | `test_stock_trend.py`、`test_golden.py --diff` |

## Stop Condition

当独立快照命令能够安全采集、所有部分/日期/盘中失败路径均不写历史、`/candidates` 复用同一提交契约、文档说明无 Tushare 运维路径，并且两个强制质量门通过时，本计划完成。不得以生成或修改 golden 快照作为通过测试的手段。

## Self-Review

- Spec coverage: 所有无 Tushare、免费源、完整性、日期、幂等、兼容和运维要求均对应到任务与测试。
- Placeholder scan: 没有未定项、延后实现或引用其他任务代替具体步骤的描述。
- Type consistency: `commit_candidate_sector_snapshot()`、`capture_snapshot()` 和 `snapshot_status()` 的签名在测试与实现步骤中一致。
- Scope control: 不安装调度器、不增加依赖、不实现不可靠历史反推、不接入需要新凭据的供应商。
