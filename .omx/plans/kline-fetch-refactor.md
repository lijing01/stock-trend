# K 线拉取公共逻辑重构计划

## 需求摘要

在不改变“今日推荐”和“个股分析”现有行为的前提下，抽取两条 K 线路径中可安全共用的纯函数和命令构造逻辑，减少日期校验、stale payload 构造和 fetcher CLI 参数拼装的重复。

不统一两条路径的业务编排：

- 今日推荐仍为缓存优先、东方财富直连、短超时、失败重试一次，并保留 stale cache 与 source evidence。
- 个股分析仍为 Tushare 优先、东方财富降级，保留股票/ETF/港股、日/周线、复权和可配置根数。

## 当前重复与边界

- `fetchers/kline.py:69-112` 与 `fetchers/kline_eastmoney.py:33-79` 存在几乎完全相同的 `latest_kline_date`、`cache_validation`、`reject_stale_payload`。
- `pipeline/runner.py:223-267` 与 `scans/stock_scanner.py:1174-1182` 都手工构造 K 线 fetcher 子进程命令。
- `pipeline/runner.py:54-68` 的通用 payload 可用性判断可以下沉，但 `stock_scanner.py:1032-1054` 的 `WYCKOFF_MIN_BARS`、推荐日覆盖和质量原因是推荐专属规则，不应被通用判断替换。

## 验收标准

1. `kline.py` 和 `kline_eastmoney.py` 不再各自定义三个重复的日期/stale 函数，对相同输入产生与重构前逐字段等价的 payload。
2. 两个入口通过同一个命令 builder 生成基础参数，同时保留原有的 provider 专属参数、顺序和标签。
3. 今日推荐的 EastMoney 命令仍精确包含 `E/D`、`4/2/1/8` 超时组合和 `--expected-date`；仅在子进程失败时重试一次。
4. 个股分析仍先调 Tushare；失败、无输出或 error payload 时再调 EastMoney，并继续传递 `asset/freq/adj/no-cache/expected-date/kline-days`。
5. 缓存命中、缓存过期、`cache_only`、stale cache 保留、source evidence、错误摘要及下游旧文件清理行为不变。
6. 不新增依赖；公共模块不发网络请求、不自行重试、不决定 provider 顺序。
7. 所有新增与现有专项测试、项目两道强制质量门均通过，golden diff 无非预期变化。

## 实施步骤

### 1. 先用特征测试锁定现有行为

在 `.claude/skills/stock-trend/tests/` 新增针对公共契约的测试，并补足现有入口断言：

- 锁定 `YYYYMMDD`/`YYYY-MM-DD`、非法日期、空数据、多行乱序数据的最新日期结果。
- 锁定 stale payload 的 `data_source/error_type/stale_data_source/record_count/error/cache_validation/data` 全字段。
- 在 `test_stock_scanner.py:290-320,441-535` 保留并强化缓存命中、超时参数、单次重试、stale cache 诊断断言。
- 在 `test_stock_trend.py:1326-1405` 和 `test_capital_flow.py:611-700` 锁定 Tushare → EastMoney 顺序、参数传递、stale/error 降级及下游输出清理。

先运行这些测试建立重构前基线；如基线已失败，先记录现有缺口，不通过改 golden 掩盖。

### 2. 抽取纯 K 线 payload 契约

新增 `.claude/skills/stock-trend/scripts/core/kline_utils.py`，只放无 I/O 纯函数：

- `normalize_kline_date(value)`
- `latest_kline_date(payload)`
- `validate_kline_coverage(payload, expected_date)`
- `reject_stale_kline_payload(payload, expected_date)`
- `is_usable_kline_payload(payload, require_ohlc=True)`

保留当前 JSON schema 和中文错误文案。`is_usable_kline_payload` 只检查通用结构、error/cache-validation 状态和最新行 OHLC；不包含推荐专属的最少 K 线数或 source evidence。

将 `fetchers/kline.py:69-112`、`fetchers/kline_eastmoney.py:33-79` 和 `pipeline/runner.py:54-68` 改为导入该模块。如现有测试或外部代码直接导入旧函数名，保留薄别名作为兼容层，暂不破坏调用面。

### 3. 抽取 fetcher 命令构造，不抽象业务编排

在 `core/kline_utils.py` 增加薄 helper：

- `build_kline_fetch_command(provider, ts_code, output_path, *, asset, freq, adj=None, no_cache=False, expected_date=None, start_date=None, limit=None, provider_args=None, python_executable=sys.executable)`

定义明确的确定性参数顺序，并对 Tushare 和 EastMoney 各写命令快照单元测试。`provider_args` 仅承载已有的 provider 专属参数，helper 不推断超时、重试或降级策略。

### 4. 迁移个股 pipeline，保留 Tushare-first 策略

在 `pipeline/runner.py:220-290` 中：

- 用公共 builder 生成 Tushare 和 EastMoney 命令。
- 用公共 payload 判断替代本地 `is_successful_kline` 实现，保留原函数名薄包装以维持测试/调用兼容。
- 不移动 `resolve_expected_date`、fallback 条件、日志、`timeouts/errors/results` 聚合和下游文件清理。

### 5. 迁移今日推荐命令拼装，保留专属缓存与证据语义

在 `scans/stock_scanner.py:1145-1236` 中仅用公共 builder 替换 `cmd` 列表的手工拼装。以 `provider_args` 显式传入：

`--em-timeout 4 --em-fallback-timeout 2 --em-host-retries 1 --fallback-timeout 8`。

不改动 `_validate_kline_cache`、`cache_only`、`_remaining_timeout`、外层一次重试、`_with_cache_verdict`、`source_result/live_attempt`和 stale cache 返回逻辑。

### 6. 删除确认无调用的重复实现并做最终回归

用 `rg` 确认旧函数实现只剩兼容别名或已完全移除；不在本次重构中顺手合并资金流、行业 K 线、投资组合或回测中的其他 fetch 逻辑。

## 验证步骤

使用项目规定的 Python 3.10 解释器，顺序执行：

1. 新增 `test_kline_utils.py` 或等价专项测试。
2. `.claude/skills/stock-trend/tests/test_stock_scanner.py`。
3. `.claude/skills/stock-trend/tests/test_capital_flow.py`。
4. `.claude/skills/stock-trend/tests/test_run_today.py`。
5. 强制门 1：`/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py`。
6. 强制门 2：`/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff`。
7. `git diff --check`。

本重构的测试默认使用 mock/临时文件，不需要真实联网。如实施时增加真实数据源 smoke test，必须按 `stock-trend` 契约用单次 `NO_PROXY/no_proxy` 在沙盒外运行，不修改全局网络配置。

## 风险与缓解

- **过度抽象导致两条业务路径被迫同质化**：公共层仅做纯 payload 契约和命令构造，provider 顺序、缓存、重试、deadline 仍由调用方控制。
- **日期格式统一后改变边界行为**：先用特征测试锁定当前结果；非法日期继续按不覆盖处理，不自动容错成有效日期。
- **命令参数顺序变化破坏 mock 或运维诊断**：builder 使用确定顺序并增加完整 argv 快照断言。
- **公共可用性判断误伤推荐缓存**：推荐继续使用 `_validate_kline_cache` 作为最终准入规则，通用 helper 不接管 `WYCKOFF_MIN_BARS` 和 stale evidence。
- **直接导入旧 helper 的隐式调用被破坏**：迁移前用 `rg` 完整检查，必要时保留一个发布周期的兼容别名。

## 停止条件

当公共层只剩纯契约与命令构造、两条编排行为由特征测试证明不变、两道强制质量门通过且 golden diff 无非预期变化时，本次重构完成。不扩展到统一所有 K 线消费者或改变数据源优先级。
