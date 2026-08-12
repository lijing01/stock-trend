# 今日推荐运行性能修复计划

> 状态：核心修复已实施；P2 去子进程化暂不需要  
> 日期：2026-08-12  
> 适用范围：`/candidates` 每日候选扫描及其复用的 `stock_scanner` 数据获取链路

## 1. 背景与基准

本计划解决“今日推荐”运行时间过长的问题，不调整推荐评分阈值，也不放宽数据日期、覆盖率、市场环境和板块持续性等质量门槛。

2026-08-12 诊断基准：

- 命令：`daily_candidates.py --json --top 30 --min-candidates 20`
- 总耗时：`259.95s`
- 扫描 20 个热点板块，共执行 5 个扩池批次
- 各批 Phase 2 原始候选数：65、63、38、62、54
- 每批依次获取 K 线、资金流和基本面
- 数据源 DNS 解析失败，东方财富重试后继续降级 AKShare
- 最终质量调整分、数据资格和板块资格同时合格的候选数为 0，因此扩池扫描了全部板块

主要原因：

1. 每个扩池批次重复请求全市场板块排行。
2. 跨板块重复股票在不同批次中重复分析。
3. 资金流和基本面在维科夫门控前获取，大量数据请求最终被漏斗淘汰。
4. 网络故障没有单次运行级熔断，失败和重试被每批、每只股票重复放大。
5. 暖缓存仍可能通过独立 Python 子进程读取，存在不必要的进程启动成本。
6. 当前输出缺少阶段耗时、请求量和缓存命中率，难以快速识别退化点。

## 2. 修复目标与质量红线

### 2.1 性能目标

- 正常网络、暖缓存：默认 `/candidates` 目标耗时不超过 30 秒。
- 正常网络、冷缓存：按候选规模记录基线，优先确保无重复请求，并将耗时控制在可解释范围内。
- 数据源完全不可用：在 30～45 秒内完成缓存降级并返回，不再等待数分钟。
- 单次执行中，全市场板块排行最多获取一次。
- 单只股票的每类数据在单次执行中最多获取一次。

### 2.2 不可放宽的质量红线

- K 线必须覆盖最近有效推荐依据交易日，不能为提速接受 T-1 旧行情。
- 休市日必须通过最近交易日判定使用合法收盘数据，不能以自然日直接推断。
- 资金、基本面、板块排行和成分股缓存必须保留来源、数据日期、抓取时间和质量状态。
- 数据源熔断后，过期或缺失数据只能进入观察池，不得生成“今日可执行”推荐。
- 市场环境、板块持续性、数据覆盖率和质量调整分门控保持不变。
- 所有兼容 JSON 字段继续保留；新增性能元数据不得破坏现有消费者。

## 3. 目标流程

```text
获取一次板块排行
  → 分批获取板块成分股
  → 全局股票去重并保留多板块归属
  → 仅为新增股票获取 K 线
  → K 线日期/根数质量检查
  → 维科夫预筛
  → 仅为通过者补充资金和基本面
  → 完整评分和推荐质量门控
  → 达到有效候选目标则停止，否则扩展下一批板块
  → 输出报告及性能审计信息
```

## 4. 分阶段实施

### P0-1：复用板块排行

涉及文件：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`

实施内容：

1. `pick_hot_sectors()` 获取排行后构建不可变的 `sector_scores`/排行上下文。
2. 将排行上下文传入 `scan_sectors()` 和 `gather_candidates()`。
3. `gather_candidates()` 仅在独立调用且调用方没有提供上下文时请求排行。
4. 扩池所有批次复用相同排行日期、来源、完整性和评分。

验收标准：

- 多批扩池时 `get_sector_rankings()` 调用次数为 1。
- 各批使用同一排行快照，不产生批次间评分漂移。
- 实时排行失败并使用旧缓存时，原有观察池降级规则继续生效。

### P0-2：单次运行全局去重

涉及文件：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`

实施内容：

1. 在扫描上下文中维护 `seen_stock_codes` 和 `analyzed_stock_codes`。
2. 新批次只将尚未分析的股票送入 Phase 2。
3. 同一股票属于多个板块时保留完整板块列表。
4. 主板块按板块可执行资格、板块评分、持续性和相对强弱择优。
5. 最终 `code` 去重语义及兼容字段保持不变。

验收标准：

- 跨板块重复股票的 K 线、资金和基本面各最多获取一次。
- 重复股票不会丢失其他板块归属证据。
- 后续更强板块出现时可以更新主板块，不受首次遇到顺序影响。

### P0-3：前置 K 线和维科夫漏斗

涉及文件：

- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`

实施内容：

1. 将当前 Phase 2 拆分为结构预筛和完整数据评分两个阶段。
2. 首先并发获取 K 线，并检查推荐依据日和最小 K 线根数。
3. 对合格 K 线执行维科夫分析。
4. 仅为维科夫门控通过者获取资金和基本面。
5. 普通 `/stock-scanner` 未开启 `--wyckoff` 时保持原有完整评分语义。

验收标准：

- `/candidates` 的资金和基本面请求数不超过维科夫通过数。
- K 线不足或维科夫不通过的股票不会调用资金和基本面 fetcher。
- 维科夫结果、复合权重和最终评分与重构前一致。

### P1-1：单次运行级数据源熔断

涉及文件：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/scripts/fetchers/sector_data.py`
- 可新增 `.claude/skills/stock-trend/scripts/core/source_health.py`
- 对应测试文件

实施内容：

1. 增加 `healthy`、`degraded`、`unavailable` 三态运行上下文。
2. 对板块排行、成分股、K 线和资金流分别记录连续失败。
3. 每类来源先进行有限小样本探测，达到阈值后停止新增实时请求。
4. 熔断后仅检查缓存，并记录 `source_unavailable`、`cache_only` 或 `data_stale`。
5. 熔断状态仅在当前进程有效，不持久化到下一次运行。
6. DNS、连接超时、HTTP 错误和返回空数据分别保留原因，便于恢复判断。

验收标准：

- 模拟 DNS 故障时，每类来源只执行有限次数探测。
- 网络完全不可用时默认扫描在 30～45 秒内结束。
- 缓存不满足日期或质量要求时不会出现可执行推荐。
- 单只股票失败不会误熔断所有不同来源。

### P1-2：父进程缓存快速路径

涉及文件：

- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/scripts/core/recommendation_quality.py`
- `.claude/skills/stock-trend/tests/test_stock_scanner.py`

实施内容：

1. 在父进程中统一校验标准输出缓存，命中后直接读取。
2. K 线缓存同时校验数据日期、根数、来源和错误状态。
3. 资金缓存按盘中/盘后 TTL 和数据质量判断。
4. 基本面缓存按既有 30 分钟/16 小时 TTL 判断。
5. 缓存无效时仍调用原 fetcher，保证 CLI 行为和降级链兼容。
6. 使用实际最近交易日作为推荐依据日，覆盖周末和节假日。

验收标准：

- 暖缓存命中时不启动对应 fetcher 子进程。
- 交易日收盘后不会误用 T-1 K 线。
- 周末和节假日不会反复请求不存在的自然日 K 线。
- 旧缓存继续进入质量门控，而不是被静默视为实时数据。

### P1-3：性能审计指标

涉及文件：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`

在 JSON `meta` 和 stderr 中增加：

- `sector_ranking_seconds`
- `sector_membership_seconds`
- `kline_seconds`
- `wyckoff_seconds`
- `capital_seconds`
- `fundamental_seconds`
- `report_seconds`
- 各类请求数、缓存命中数、失败数和熔断数
- 扫描批次数、原始候选数、全局去重后候选数、维科夫通过数和最终有效数

验收标准：

- 各阶段耗时之和与总耗时误差在合理范围内。
- JSON 保留现有 `meta`、`candidates`、`recommendations` 和分层字段。
- 性能信息不改变评分、排序和报告资格。

### P2：减少 Python 子进程启动成本

在 P0/P1 稳定后实施，避免与流程重构同时扩大风险。

涉及文件：

- K 线、资金和基本面 fetcher
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- fetcher 和 scanner 对应测试

实施内容：

1. 将 fetcher 核心逻辑提取为可直接导入的函数。
2. CLI 脚本保留为参数解析和输出薄封装。
3. scanner 在线程池内直接调用函数，减少解释器启动和模块重复加载。
4. 保留每任务超时、结构化异常和独立降级结果。

验收标准：

- CLI 输出和退出码保持兼容。
- 直接函数调用与 CLI 在相同 fixture 下产生等价数据。
- 单个 fetcher 异常不会终止整个候选扫描。

## 5. 测试策略

### 5.1 单元和回归测试

新增或补充以下场景：

1. 多批扩池只获取一次板块排行。
2. 跨板块重复股票只分析一次，并保留多板块归属。
3. 后续更强板块可成为重复股票的主板块。
4. K 线过期、K 线不足和维科夫失败时不获取昂贵维度。
5. 连续 DNS 失败后触发熔断，后续任务直接缓存降级。
6. 熔断后旧数据只能进入观察池。
7. 暖缓存路径不启动子进程。
8. 交易日盘中、盘后、周末及节假日的推荐依据日正确。
9. 性能审计字段完整且不破坏 JSON 兼容性。

### 5.2 确定性性能测试

使用 mock 延迟和固定 fixture，不以公网实时速度作为自动测试判据：

- 20 个板块；
- 约 250 个原始股票；
- 30% 跨板块重复；
- 正常、慢响应和完全不可用三种来源状态；
- 断言调用次数、缓存命中次数和最大理论等待时间。

### 5.3 仓库质量门

每次修改 `.claude/skills/stock-trend/scripts/` 下 Python 文件后必须运行：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

同时运行定向测试：

```bash
python3 -m unittest .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
```

不得仅为消除失败而重新生成 golden。只有逐项确认数值或输出变化符合预期后才允许更新，并在提交信息中说明原因。

## 6. 交付顺序

建议拆分为五个可独立审查的提交：

1. 复用板块排行。
2. 全局股票去重与增量扩池。
3. K 线/维科夫前置漏斗。
4. 数据源熔断、父进程缓存快速路径和性能指标。
5. fetcher 去子进程化重构。

前三项完成后应重新执行冷缓存、暖缓存和断网基准；达到目标后再判断是否需要第五项。

## 7. 完成定义

以下条件全部满足后，本计划才可标记完成：

- 性能目标在固定 fixture 和一次实际环境基准中得到验证。
- 网络故障不会导致无界重试或全板块、全股票重复等待。
- 暖缓存能绕过不必要的子进程。
- 推荐质量门槛、数据日期规则和降级语义没有放宽。
- 所有定向测试、主质量门和 golden diff 通过。
- JSON 和报告中可以审计数据源、缓存命中、熔断及各阶段耗时。

## 8. 实施结果（2026-08-13）

已完成：

- P0-1：候选流程复用首次获取的板块排行上下文，扩池不再重复拉取全市场排行。
- P0-2：跨批次按股票代码增量去重，同一股票只进入一次深度分析，并保留板块归属列表。
- P0-3：K 线和维科夫漏斗前置，仅对通过者获取资金和基本面。
- P1-1：板块成分、K 线、资金和基本面增加单次运行级健康状态与缓存降级。
- P1-2：有效资金、基本面和 K 线输出缓存增加父进程快速路径。
- P1-3：JSON `meta.performance` 增加阶段耗时、批次、原始/去重候选数、维科夫通过数及数据源状态。

真实断网/缓存环境基准：

| 指标 | 修复前 | 第一轮 | 最终 |
|---|---:|---:|---:|
| 总耗时 | 259.95s | 102.14s | 45.36s |
| 板块排行请求 | 每批重复 | 1 次 | 1 次 |
| 原始批次候选任务 | 282 | 282 | 282 |
| 去重后股票任务 | 未统计 | 246 | 246 |
| 资金/基本面分析对象 | 所有候选 | 维科夫通过者 33 | 维科夫通过者 33 |
| 基本面阶段耗时 | 未单列 | 55.901s | 0.011s |

最终总耗时比修复前降低约 82.6%，达到数据源不可用时 30～45 秒目标的边界。运行仍输出 0 个有效候选，说明提速没有绕过数据新鲜度和推荐资格门控。

P2 去子进程化暂缓：现有实现已达到断网目标，继续提取 fetcher 核心函数会显著扩大重构面。后续只有在正常网络冷缓存基准仍不达标时再启动 P2。

---

本计划用于系统可靠性与性能改进。股票分析结果仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。
