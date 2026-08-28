# Weekly Market Data Reliability Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ] ) syntax for tracking.

**Goal:** 修复周报中同花顺实时行业热力因 DNS 失败后静默为空、龙虎榜快照显示 0 天且无法区分未运行/无数据/失败的问题，使周报在数据源异常时仍可运行、明确披露数据覆盖与可信度，并保留现有公共调用兼容性。

**Architecture:** 在现有 core.source_health 证据契约上增加行业热力和龙虎榜的状态封装。行业热力采用“同花顺优先、东方财富独立兜底”，龙虎榜由每日采集任务写入成功快照和逐日状态 sidecar；周报同时读取快照与状态，并在龙虎榜不可用时重归一化评分权重，避免把固定中性分伪装成验证信号。板块映射允许 LHB 在明确标记 stale 的情况下使用旧缓存，但不会把旧缓存标为新鲜数据。

**Tech Stack:** Python 3、AKShare、现有东方财富 HTTP fetcher、core.source_health、pytest、Markdown/HTML 报告；不新增依赖，不修改全局代理或凭据配置。

---

## 1. 已确认的根因与边界

### 1.1 证据

- .claude/skills/stock-trend/scripts/analysis/ths_theme.py:44 的 fetch_industry_data() 直接调用 ak.stock_board_industry_summary_ths()，异常统一吞掉并返回 []。
- 当前 AKShare 实现访问 http://q.10jqka.com.cn/thshy/index/；运行环境的 NO_PROXY 包含同花顺域名，默认执行上下文无法解析该域名，因此出现 NameResolutionError。
- 同一诊断窗口中，启用网络的既有 fetcher 能取到同花顺行业 90 行、东方财富龙虎榜 32 行，说明“供应商永久无数据”不是主因；应修复运行时证据、兜底和运维可见性。
- .claude/skills/stock-trend/scripts/analysis/weekly_report.py:65 的 load_lhb_snapshots() 只读 .cache/stock-trend/lhb_snapshots/YYYY-MM-DD.json，不执行实时龙虎榜采集；当前该目录不存在，所以周报显示 0 天。
- .claude/skills/stock-trend/scripts/analysis/lhb_tracker.py:49 只有在 run_lhb_analysis() 返回板块后才落盘，失败、无数据、映射失败均不留下状态记录。
- .cache/stock-trend/stock_sector_map.json 的 built_at 为 2026-07-07，sector_mapper.py 的有效期为 168 小时；该映射已过期，是龙虎榜恢复后的潜在二级故障。
- 当前周报使用固定 10% LHB 分量，即使 lhb_snapshots=[] 也仍使用 lhb_score=50，导致“无 LHB 验证”没有被数学和报告层明确表达。

### 1.2 修复边界

- 保留 fetch_industry_data()、fetch_lhb_jgmmtj()、save_daily_snapshot() 等现有列表/快照兼容接口；新增 evidence/status 接口供周报和跟踪器使用。
- 复用 classify_failure()、live_attempt()、source_result()，不重新发明一套错误码。
- 不在代码里改写 shell 的 HTTP_PROXY、HTTPS_PROXY、NO_PROXY，不保存代理凭据；网络配置问题通过状态、日志和 runbook 暴露。
- 不把旧缓存、未运行、非交易日空结果称为“实时成功”。
- 不为消除测试差异而重生成 golden snapshot；只有确认输出契约变化是本修复的预期结果后，才更新对应快照并记录原因。

### 1.3 停止条件

完成必须同时满足：

- DNS/HTTP/解析失败能得到稳定的 reason 和 status，而不是只得到空列表。
- 同花顺失败时，东方财富兜底成功可以生成可评分的行业数据；两者都失败时周报仍可输出历史部分并给出警告。
- LHB 每次尝试都有状态记录；周报能区分 not_run、live_success、no_data、error、mapping_error 和旧快照。
- 无有效 LHB 快照时不使用固定 10% 中性分；报告披露权重已重归一。
- 目标单测、项目质量门禁、编译检查、diff 检查和最小 live smoke 均通过。

---

## 2. 数据状态契约

先在测试中锁定以下契约，再实现生产代码。内部 provider 可继续用 source_result({"payload": ...}, live_attempt(...))；下列是业务层返回给周报/跟踪器的稳定字段。

### 2.1 行业热力

fetch_industry_data_with_evidence() 和周报内部的 fetch_current_industry_data() 统一返回：

~~~python
{
    "data": [],  # 行业热力行，字段兼容现有 score_industries 输入
    "status": "live_success",  # live_success | no_data | error
    "source": "ths_akshare",  # ths_akshare | eastmoney_push2 | none
    "live_attempt": {
        "attempted": True,
        "provider_attempts": 1,
        "reason": "",
        "cache_used": False,
        "stale": False,
        "status": "success",
    },
    "errors": [],
}
~~~

规则：

- live_success 仅表示本次实时请求获得有效行；source 明确标出实际供应商。
- 同花顺异常后使用东方财富时，status=live_success、source=eastmoney_push2，并保留同花顺失败信息到 errors。
- 所有 provider 都返回空列表且至少一次请求正常完成时为 no_data；请求异常、DNS、超时、HTTP 或解析失败且没有可用兜底时为 error。
- 现有无 evidence 列表接口只返回 data，不改变调用方行为。

### 2.2 龙虎榜采集状态

每日写入 .cache/stock-trend/lhb_snapshots/status/YYYY-MM-DD.json：

~~~json
{
  "date": "YYYY-MM-DD",
  "attempted_at": "YYYY-MM-DD HH:MM:SS",
  "status": "live_success",
  "requested_date": "YYYYMMDD",
  "data_date": "YYYYMMDD",
  "total_lhb_stocks": 32,
  "total_sectors": 4,
  "mapping_stale": false,
  "live_attempt": {
    "attempted": true,
    "provider_attempts": 1,
    "reason": "",
    "cache_used": false,
    "stale": false,
    "status": "success"
  },
  "failure_reasons": [],
  "errors": []
}
~~~

允许状态：

- live_success：拿到 LHB 明细并完成板块聚合；可同时有 mapping_stale=true，但报告必须显示。
- no_data：供应商请求完成但指定日期及有限回溯日期均无明细，典型为非交易日。
- error：DNS、超时、HTTP、解析等请求失败，且没有成功 payload。
- mapping_error：LHB 明细成功，但无法得到可用映射。
- 没有 sidecar 的日期在周报中记为 not_run；有旧 JSON 但无 sidecar 的日期记为 legacy_snapshot，可参与历史统计但不能宣称有本次采集证据。

---

## 3. 实施任务

### Task 1 — 先建立回归测试和固定夹具

Files:

- Modify .claude/skills/stock-trend/tests/test_ths_theme.py
- Modify .claude/skills/stock-trend/tests/test_lhb_tracker.py
- Modify .claude/skills/stock-trend/tests/test_weekly_report.py
- Modify .claude/skills/stock-trend/tests/test_sector_mapping.py
- 若现有测试没有合适的临时目录夹具，在测试文件内使用 tmp_path 和 monkeypatch，不要写入真实 .cache。

Steps:

- [ ] 增加同花顺 DNS 异常测试：mock ak.stock_board_industry_summary_ths 抛出包含 NameResolutionError/getaddrinfo 的异常，断言 evidence 的 status=error、reason=dns、data=[]，并确认旧的 fetch_industry_data() 仍只返回列表。
- [ ] 增加同花顺失败、东方财富成功的周报测试：mock 两个 provider，断言输出行数大于 0、source=eastmoney_push2、errors 保留 THS 失败、行业状态为 live_success。
- [ ] 增加两 provider 都不可用测试：断言周报聚合不抛异常，meta 为 status=error，报告文本和 HTML 都包含数据不可用警告。
- [ ] 增加 LHB “无数据”和“DNS 失败”测试：分别断言 no_data 与 error/dns，日期回溯最多 5 个日历日期，不产生无界重试。
- [ ] 增加 LHB 状态 sidecar 测试：成功、无数据、映射失败分别检查 sidecar 字段；没有 sidecar 的日期检查为 not_run/旧快照状态。
- [ ] 增加过期映射测试：load_mapping() 仍拒绝过期缓存，load_mapping(allow_stale=True) 返回相同映射并设置 meta.stale=True；空映射不得被当作可用 stale 映射。
- [ ] 增加评分测试：没有任何有效 LHB 快照时，四个热度/持续性分量按可用权重重归一，结果不含固定中性 10% 分；有有效快照时保留原 10% LHB 权重。
- [ ] 增加兼容性测试：既有列表接口、aggregate_sectors(market, lhb, industries) 三参数调用和 save_daily_snapshot() 返回形状继续有效。

建议的关键断言形状：

~~~python
def test_lhb_unavailable_renormalizes_weights():
    scored = aggregate_sectors(
        {"2026-08-27": [{
            "name": "测试行业",
            "code": "BK0001",
            "hot_score": 80,
            "change_pct": 2,
            "net_flow": 100000000,
            "leader": "测试股",
        }]},
        [],
        [],
        lhb_meta={"available_days": 0, "status": "error"},
    )
    assert scored
    assert scored[0]["lhb_status"] == "error"
    assert scored[0]["score_weights"]["lhb"] == 0
    assert scored[0]["score_weights"]["base_total"] == 0.90
~~~

验证：

~~~bash
python3 -m pytest .claude/skills/stock-trend/tests/test_ths_theme.py \
  .claude/skills/stock-trend/tests/test_lhb_tracker.py \
  .claude/skills/stock-trend/tests/test_weekly_report.py \
  .claude/skills/stock-trend/tests/test_sector_mapping.py -q
~~~

测试在实现前允许因新接口不存在而失败；实现完成后必须恢复全绿。

### Task 2 — 为同花顺行业热力增加 evidence，并接入独立东方财富兜底

Files:

- Modify .claude/skills/stock-trend/scripts/analysis/ths_theme.py
- Modify .claude/skills/stock-trend/scripts/analysis/weekly_report.py
- Reuse .claude/skills/stock-trend/scripts/core/source_health.py
- Reuse .claude/skills/stock-trend/scripts/fetchers/sector_data.py

Steps:

- [ ] 在 ths_theme.py 增加 evidence-aware 的行业 fetch helper；把已有 DataFrame 转换逻辑集中到该 helper，记录 provider attempt 次数、异常类型、classify_failure() 的 reason，并保留 stderr 诊断。
- [ ] 保留 fetch_industry_data() 作为兼容包装，只返回 evidence 中的 data，不让旧调用方收到 wrapper。
- [ ] 在 weekly_report.py 增加 fetch_current_industry_data()：先调用 THS evidence helper；当 THS 为 DNS/timeout/HTTP/parse/empty 失败时调用既有 get_sector_rankings(with_evidence=True) 的东方财富路径。
- [ ] 兜底只接受东方财富返回的有效行业行，转换为 score_industries() 所需字段：name、change_pct、net_flow、total_amount、up_count、down_count、空的领涨股字段；main_force_net 的单位保持为元，不重复乘缩放因子。
- [ ] 检查 fallback meta，不能把同花顺内部 fallback 当成独立东方财富成功；source 标签必须是 eastmoney_push2，否则继续按不可用处理。
- [ ] 两个 provider 均失败时返回 data=[] 加完整状态，不抛出异常；若均正常但返回空，则使用 no_data。
- [ ] 主流程调用新 helper；JSON/Markdown/HTML meta 增加 industry_status、industry_source、industry_live_attempt、industry_errors。
- [ ] 当前行业数据为空但历史市场快照不为空时继续生成周报；当前数据和历史都为空时保留现有“无数据”退出语义，但输出可诊断状态。

不做的事情：

- 不直接修改 AKShare 包源码。
- 不新增 requests/proxy 依赖。
- 不在每次周报运行时无限重试同花顺；provider 重试次数沿用现有东方财富 bounded policy，并将 DNS 失败快速分类。

### Task 3 — 让 LHB fetcher 返回日期和失败证据

Files:

- Modify .claude/skills/stock-trend/scripts/fetchers/longhubang_agg.py
- Modify .claude/skills/stock-trend/scripts/analysis/lhb_tracker.py

Steps:

- [ ] 增加 fetch_lhb_jgmmtj_with_evidence(date_str=None)；旧 fetch_lhb_jgmmtj() 只返回 data 列表。
- [ ] 请求顺序为目标日期、随后最多 4 个前一日历日；目标日期返回空时才回溯。遇到 DNS/连接/HTTP/解析异常时记录 reason 并停止继续请求同一故障源，避免在 DNS 故障时制造 5 倍等待。
- [ ] 成功返回时记录 requested_date 和实际 data_date；空响应但请求完成时为 no_data；异常时为 error，使用 classify_failure()，不再静默 return []。
- [ ] 给 run_lhb_analysis() 增加内部 status/meta 传播：fetch 失败为 error，明细为空为 no_data，映射不可用为 mapping_error，聚合成功为 live_success。
- [ ] 映射读取使用后续 Task 5 的 get_mapping(allow_stale=True)；若只拿到 stale 映射，设置 mapping_stale=True，但不得把 live_attempt.stale 误设为网络缓存命中。
- [ ] 仍保留原 result["sectors"] 和 result["meta"]["total_lhb_stocks"] 等字段，确保现有 HTML/CLI 不破坏。

### Task 4 — 每次 LHB 尝试落状态 sidecar，修复 tracker 的“静默缺席”

Files:

- Modify .claude/skills/stock-trend/scripts/analysis/lhb_tracker.py
- Modify .claude/skills/stock-trend/tests/test_lhb_tracker.py

Steps:

- [ ] 增加 STATUS_DIR = SNAPSHOT_DIR / "status" 和私有 _write_status(date_str, status)；目录创建和 JSON 编码沿用现有缓存风格。
- [ ] 将现有保存逻辑拆成 collect_daily_snapshot(date_str=None)：只调用一次 run_lhb_analysis()，无论成功、无数据、请求失败、映射失败都写一份 sidecar。
- [ ] live_success 才写原有 YYYY-MM-DD.json 信号快照；no_data、error、mapping_error 只写状态文件，避免空快照伪装成有效信号。
- [ ] 保留 save_daily_snapshot() 作为兼容包装，成功返回原 snapshot dict，其他状态继续返回空 dict；状态信息通过 sidecar 和新增 tracker 结果可查。
- [ ] main() 改调用 collect_daily_snapshot()，CLI 输出明确显示：状态、请求日期、实际数据日期、股票数、板块数、映射是否 stale、失败 reason。
- [ ] 状态写入失败不能覆盖原始采集结果；打印 stderr 并让返回结果保留 status_write_error，测试中用不可写路径覆盖该分支。

### Task 5 — 处理映射缓存过期，但保持“新鲜/陈旧”语义

Files:

- Modify .claude/skills/stock-trend/scripts/fetchers/sector_mapper.py
- Modify .claude/skills/stock-trend/tests/test_sector_mapping.py

Steps:

- [ ] 将 load_mapping(allow_stale=False) 改为：缺失、解析失败、空映射仍返回 None；过期且未允许 stale 返回 None；过期且 allow_stale=True 返回映射副本并写入 meta.stale=True、meta.age_hours。
- [ ] 将 get_mapping(rebuild=False, allow_stale=False) 改为先取 fresh；fresh 不存在时，allow_stale=True 再取 stale；最后才 build。build 失败时不得用空结果覆盖已有缓存。
- [ ] get_stock_sectors() 默认保持严格 fresh 行为；只有 LHB pipeline 显式传 allow_stale=True。
- [ ] 解析 ISO 时间时兼容现有无时区值；负年龄视为 fresh，避免系统时钟轻微漂移导致误判。
- [ ] stale 映射参与计算的报告必须标记 mapping_stale=true，并在 LHB 状态中记录受影响日期。

### Task 6 — 周报读取状态、修正评分权重并输出覆盖率

Files:

- Modify .claude/skills/stock-trend/scripts/analysis/weekly_report.py
- Modify .claude/skills/stock-trend/tests/test_weekly_report.py

Steps:

- [ ] 增加 load_lhb_snapshot_bundle(days=10)，返回 snapshots、available_days、attempted_days、status_days、failure_reasons、mapping_stale_days；保留 load_lhb_snapshots() 兼容并只返回列表。
- [ ] 读取顺序按日期窗口逐日检查：有效 JSON、对应 sidecar、两者缺失。旧 JSON 无 sidecar 记为 legacy_snapshot；无任何记录记为 not_run。
- [ ] aggregate_sectors() 增加可选 lhb_meta=None，不破坏原三参数调用。按有效 snapshot 是否大于 0 决定 LHB 权重：
  - 有效 LHB：维持原 0.30/0.25/0.25/0.10/0.10；
  - 无有效 LHB：LHB 权重为 0，将 avg_hot/frequency/latest_hot/trend 的总分除以 0.90，再限制到 0–100；
  - no_data、error、mapping_error 和 not_run 都不得生成中性 LHB 信号。
- [ ] 每条评分行增加 lhb_status 和 score_weights，至少能回溯该行是否使用过 LHB 权重。
- [ ] 主流程使用行业 evidence helper 和 LHB bundle；meta 增加：industry_status、industry_source、industry_errors、lhb_available_days、lhb_attempted_days、lhb_status_days、lhb_failure_reasons、mapping_stale_days、warnings、score_weights。
- [ ] Markdown 和 HTML 的数据概览区显示行业来源、LHB 有效/尝试天数、未运行天数、失败原因、stale 映射天数；没有 LHB 时明确写“本周无有效龙虎榜验证，周分已重归一，不代表机构资金确认”。
- [ ] JSON 输出保留现有 strong、active、lhb_buy 字段，并新增完整 meta；lhb_buy 只能来自有效 LHB snapshot。
- [ ] 报告文本继续是学习和参考用途，不新增短线/盘中交易建议；数据异常时降低语义强度，不把历史快照写成实时数据。

### Task 7 — 文档和运维 runbook

Files:

- Modify docs/usage-guide.md
- 如项目已有合适的诊断文档，优先在该文档补充；否则新增 docs/weekly-data-reliability.md

Steps:

- [ ] 说明周报不会自动产生历史 LHB 快照；部署/定时任务需每日运行：
  python3 .claude/skills/stock-trend/scripts/analysis/lhb_tracker.py --snapshot-only。
- [ ] 说明 sidecar 状态、有效快照和 legacy_snapshot/not_run 的含义，以及如何读周报 JSON 的 coverage 字段。
- [ ] 增加故障排查命令，至少覆盖 q.10jqka.com.cn、datacenter-web.eastmoney.com 的 DNS/HTTPS 可达性、代理和 NO_PROXY 的有效值；命令只读，不打印凭据。
- [ ] 明确配置建议：由运行环境决定是修复 DNS、修正 NO_PROXY 路由还是允许东方财富兜底；仓库代码不持久化代理配置。
- [ ] 说明东方财富兜底不是同花顺原始字段的完全等价物，报告中必须保留 source 标签；实时行业与 LHB 仍可能因交易时段/非交易日为 no_data。
- [ ] 给出完整恢复验证顺序：先运行单日 tracker，再运行 weekly JSON/HTML，检查 available_days 和 industry_source，最后查看生成报告。

---

## 4. 验证顺序与命令

实现每个任务后按依赖顺序验证，不先做 live smoke 再补单测。

### 4.1 目标回归测试

~~~bash
python3 -m pytest .claude/skills/stock-trend/tests/test_ths_theme.py \
  .claude/skills/stock-trend/tests/test_lhb_tracker.py \
  .claude/skills/stock-trend/tests/test_weekly_report.py \
  .claude/skills/stock-trend/tests/test_sector_mapping.py -q
~~~

### 4.2 仓库强制质量门禁

~~~bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
python3 -m compileall -q .claude/skills/stock-trend/scripts
git diff --check
~~~

若 golden 因本次预期的 meta/警告字段变化失败：

1. 先逐项确认差异来自本计划中的状态披露或权重修正；
2. 保留一条回归测试锁定新契约；
3. 只更新受影响 snapshot，不更新无关 golden；
4. 在提交说明中写明数值/输出变化原因。

### 4.3 最小 live smoke

在允许网络的执行环境运行，不把网络失败误判为代码测试失败：

~~~bash
python3 -c 'import sys, json; from pathlib import Path; sys.path.insert(0, str(Path(".claude/skills/stock-trend/scripts"))); from analysis.ths_theme import fetch_industry_data_with_evidence; r=fetch_industry_data_with_evidence(); print(json.dumps({"status": r["status"], "source": r["source"], "rows": len(r["data"])}, ensure_ascii=False))'
python3 -c 'import sys, json; from pathlib import Path; sys.path.insert(0, str(Path(".claude/skills/stock-trend/scripts"))); from fetchers.longhubang_agg import fetch_lhb_jgmmtj_with_evidence; r=fetch_lhb_jgmmtj_with_evidence("20260828"); print(json.dumps({"status": r["status"], "data_date": r.get("data_date"), "rows": len(r["data"])}, ensure_ascii=False))'
python3 .claude/skills/stock-trend/scripts/analysis/lhb_tracker.py --snapshot-only
python3 .claude/skills/stock-trend/scripts/analysis/weekly_report.py --weeks 1 --json --html
~~~

验收字段：

- 行业：status=live_success 且 source 为 ths_akshare 或 eastmoney_push2，或在真实网络失败时明确为 error/no_data。
- LHB：状态 sidecar 存在；成功时 available_days 增加，非交易日时为 no_data，网络失败时为 error，不能悄悄变成“0 天”。
- 周报 JSON 的 meta 与 HTML/Markdown 概览一致。
- 运行前后不修改全局 shell 配置、不新增依赖、不写入 reports/ 之外的非预期文件；检查 git status --short 只包含本任务文件和明确的运行缓存/报告产物。

---

## 5. 提交与交付拆分

建议按可回滚边界提交：

1. test: lock weekly source failure contracts
2. fix: add industry source evidence and fallback
3. fix: persist lhb collection status
4. fix: allow explicitly stale sector mapping
5. fix: make weekly lhb coverage and weights truthful
6. docs: document weekly data reliability runbook

每个提交都必须带对应测试证据；不把真实网络 smoke 结果当作离线单测替代。最终交付应列出改动文件、状态契约、评分变化、验证命令和仍受外部 DNS/代理影响的风险。
