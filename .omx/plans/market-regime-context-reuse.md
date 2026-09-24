# 今日复盘市场上下文供今日推荐复用

## 目标与范围

把 `daily_candidates.py` 中市场上下文的读取、旧缓存兼容、市场解释补建和推荐字段投影收拢为一个有明确输入输出的接口。只调整市场上下文交接；不改变市场评分、推荐门槛、正式快照格式或复盘报告。

现状：`market_regime.py:1017-1037` 保存和读取原始复盘上下文；`daily_candidates.py:3024-3131` 另行读取 JSON、兼容两种旧结构并生成推荐视图；`run_today.py:29-75` 在启动候选扫描前校验本次复盘上下文确已落盘。

## 设计决定

在 `analysis/market_regime.py` 提供 `load_recommendation_context(path=None, *, today=None)`，由该模块统一负责原始上下文到推荐视图的转换；`path` 在调用时解析，方便隔离测试。保留现有 `load_context()` 的 `--no-refresh` 语义。新接口仅内存转换，不写回缓存，读取失败返回 `None`。`daily_candidates.load_regime_context()` 暂留薄包装，兼容现有调用与测试，并删除原地的两段旧结构兼容实现和字段投影。

不把 `build_recommendation_policy()` 放入公共接口：它是推荐决策，复盘只生产市场事实。也不改 `run_today.py` 对本次刷新持久化结果的校验。

## 实施步骤

1. **先锁行为。** 在 `test_daily_candidates.py` 和 `test_market_explanation_reporting.py` 补足/确认同日旧北向标记、其他 partial 组件、盘中旧结构、无效 JSON、缺文件及旧市场解释补建的回归断言。断言旧数据只在内存变换，缓存字节不变；盘中混合 `score` 不重算。
2. **建立单一转换接口。** 将 `daily_candidates.py:3024-3131` 的兼容及投影逻辑迁入 `market_regime.py` 的新接口，复用该模块的 `compute_regime()` 和 `analysis.market_explanation.build_market_explanation()`。保留当前推荐视图的全部键及 `context_sha256` 计算口径；摘要基于兼容后的完整内存上下文，且不能因 `load_context()` 删除旧字段而变化。
3. **替换调用并收拢测试。** 推荐侧薄包装传入当前 `CACHE_DIR / "market_regime.json"`；相关单元测试直接覆盖新接口，同时保留至少一条通过 `daily_candidates.load_regime_context()` 的集成断言。保留 `_legacy_market_regime()` 的正式快照投影，不把 `market_explanation` 或新字段写入正式快照。

## 验收标准

- 新旧有效缓存的推荐视图在字段、值、`context_sha256`、解释内容和推荐门控结果上与改前一致；旧北向标记仅在规定条件下由 partial 转 good。
- 盘中旧缓存只补质量/审计字段，保留原混合分；历史或其他 partial 数据不会被放行；任何读取均不改写缓存。
- 缺文件、损坏 JSON 返回 `None`，候选流程仍按缺失市场上下文降级；统一入口仍拒绝本次复盘未成功落盘的情况。
- 正式推荐快照继续采用旧字段白名单，快照内容摘要不因本次重构变化。

## 验证与风险

先运行相关的 `test_daily_candidates.py`、`test_market_explanation_reporting.py`、`test_run_today.py`；修改脚本后按仓库要求运行 `python3 .claude/skills/stock-trend/tests/test_stock_trend.py` 和 `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`。本机执行时用 Python 3.10 解释器替换 `python3`。再运行 `git diff --check`。若 golden 出现差异，逐项确认是否意外改变输出，不能直接更新快照。

主要风险是 `load_context()` 会删除退役字段，而现有推荐摘要针对兼容后的完整上下文计算；新接口应独立读取原始 JSON 并用固定样例比较摘要。另一个风险是测试对 `daily_candidates.CACHE_DIR` 打补丁；薄包装显式传路径可保持测试隔离。
