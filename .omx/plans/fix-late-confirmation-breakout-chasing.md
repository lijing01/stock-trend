# 修复计划：确认滞后与突破后追高

## 目标与范围

目标是保留维科夫结构识别的完整历史，同时把“结构仍有效”和“当前仍适合入场”拆成两套资格，防止旧 LPS/Spring 和已经远离突破位的 JAC 继续占据推荐前列。

本次只调整候选发现后的入场资格、排序、审计和报告展示，不改变市场环境硬门控、板块持续性逻辑、新闻影子层，也不引入新依赖。

## 已确认的问题

- 结构事件当前允许 Spring/ST/JAC 保留 8 根、LPS/BU 保留 10 根 K 线；这些窗口适合结构追踪，但对入场资格过宽，见 `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:147-150`。
- `is_buy_point()` 直接复用结构事件最大年龄作为买点新鲜度，见 `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:972-981`。
- 首次确认的 JAC 即可通过选股漏斗；`post_lps_reconfirmation` 当前只影响三级奖励，不影响基础候选资格，见 `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:176-198` 与 `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:1894-1904`。
- 最终候选门控没有检查当前价相对触发价或 ATR 的偏离，见 `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:239-250`。
- 当前排序主要使用质量分加买点奖励；旧信号和已扩张信号仍可能靠动量、量价与奖励排在前面，见 `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:147-200`、`:2108-2126`。

## 设计原则

1. 结构识别与入场资格分离：不缩短结构事件历史，只收紧可执行窗口。
2. 年龄和价格偏离双门控：新确认但已经大幅拉升的 JAC 也不能直接放行。
3. 安全门控不可被高分、买点奖励或新闻覆盖。
4. 被拦截的标的继续保留在观察池，明确给出等待回踩或信号过期原因。
5. 所有新判断必须使用决策时点已有的 K 线、触发价和 ATR，避免前视偏差。

## 建议的初始策略

### 1. 保留结构窗口

继续使用现有 `EVENT_MAX_AGE` 作为结构展示和事件历史窗口，不直接修改 8/10 根 K 线的定义。

### 2. 新增独立执行窗口

增加集中定义的 `EXECUTION_MAX_AGE`：

| 信号 | 可执行最大年龄 | 超过后的处理 |
|---|---:|---|
| Spring / ST | 2 根 | 观察池，标记 `wyckoff_signal_stale` |
| Pre-markup | 1 根 | 等待重新确认 |
| 首次 JAC | 1 根 | 只能等待回踩，不能直接进入“今日可执行” |
| LPS / BU | 3 根 | 观察池，等待新的 LPS/BU |
| JAC/BU 后再确认 | 2 根 | 超龄后回到观察池 |

这些值作为保守首版参数；结构仍可在报告中显示到原来的 8/10 根窗口。

### 3. 新增价格扩张门控

从当前收盘价、事件触发收盘价和决策时点 ATR 计算：

- `trigger_extension_pct = current_close / trigger_close - 1`
- `trigger_extension_atr = (current_close - trigger_close) / current_atr`

初始分级：

- `entry_fresh`：偏离不超过 1.0 ATR 且不超过 5%。
- `entry_wait_pullback`：偏离在 1.0–1.5 ATR 或 5%–8%，只能进入等待触发。
- `entry_overextended`：偏离超过 1.5 ATR 或 8%，只能进入观察池。
- 触发价或 ATR 缺失：不得视为新鲜，进入观察池并标记 `entry_distance_unknown`。

百分比是绝对安全上限，ATR 用于适配不同波动率；两者任一越过硬上限即拦截。

### 4. 收紧首次 JAC 的资格

- `confirmed JAC + post_lps_reconfirmation=false`：保留结构标签，但只允许“等待回踩确认”。
- 已形成低量回踩并重新站稳的 LPS/BU，或 `post_lps_reconfirmation=true` 的三级信号，才可进入可执行资格判断。
- 现有 `retest_pending`、`failed_breakout` 继续拥有更高优先级的否决权。

### 5. 排名不再奖励旧结构

保持 `raw_composite_score` 和 `quality_adjusted_score` 不变，新增独立的入场时机字段：

- `entry_timing_status`
- `entry_timing_score`
- `signal_age_penalty`
- `trigger_extension_penalty`

候选排序顺序调整为：可执行资格 → 入场时机状态 → 原有 `execution_priority_score`。过期或过度扩张的信号不得获得买点奖励，并在观察池中排在新鲜结构之后。

## 实施步骤

### 阶段 A：锁定现有行为与诊断样本

1. 在 `.claude/skills/stock-trend/tests/test_wyckoff.py` 补充现状回归样本，覆盖 age=0/1/2/3/8/9 和首次 JAC、LPS、回踩再确认。
2. 固化最近报告中的代表案例：旧 LPS、高位 JAC、正常新鲜 LPS、回踩失败 JAC。
3. 测试必须先证明现有逻辑会放行“结构有效但入场过晚”的案例，再开始修改。

### 阶段 B：建立统一入场时机判定

1. 在 `.claude/skills/stock-trend/scripts/analysis/wyckoff.py` 增加纯函数 `classify_entry_timing()`，输入短线信号、当前收盘、触发收盘和 ATR，输出状态、原因和全部审计数值。
2. 将结构有效期和执行有效期明确拆开；`is_buy_signal()` 继续表达结构资格，新增 `is_executable_buy_signal()` 表达入场资格。
3. 所有阈值集中定义并带策略版本，禁止散落到 scanner 或报告代码。

### 阶段 C：接入选股漏斗和推荐硬门控

1. 在 `.claude/skills/stock-trend/scripts/scans/stock_scanner.py` 构造候选时写入 `entry_timing`，确保只使用当日及以前数据。
2. 在 `.claude/skills/stock-trend/scripts/scans/daily_candidates.py` 的 `_candidate_gate_pass()` 前加入入场资格检查。
3. 新增标准原因码：`wyckoff_signal_stale`、`first_jac_wait_retest`、`entry_overextended`、`entry_distance_unknown`。
4. 高质量分、板块强度和买点奖励均不能越过这些原因码。

### 阶段 D：调整奖励与排序

1. `apply_buy_point_priority()` 仅对 `entry_fresh` 且满足严格等级的候选应用奖励。
2. 新鲜但处于等待区间的候选保留原始质量分，不加奖励。
3. 观察池先按 `entry_fresh`、`entry_wait_pullback`、`entry_overextended/stale` 分组，再按原有优先分排序。
4. 在 `.claude/skills/stock-trend/scripts/core/candidate_score_ledger.py` 记录年龄、价格偏离、ATR 偏离、门控结果和奖励是否被抑制。

### 阶段 E：增强报告可解释性

在 Markdown、HTML 和 JSON 中增加：

- 事件日期、确认日期、信号年龄；
- 触发价、当前价、距触发价百分比和 ATR 倍数；
- `新鲜 / 等待回踩 / 过度扩张 / 已过期 / 数据未知`；
- 被降级到观察池的具体原因。

报告顶部增加“入场时机审计”：旧信号数、过度扩张数、首次 JAC 等待回踩数。观察池中的旧 LPS 不再显示为“潜在可执行买点”。

### 阶段 F：历史回放与阈值校准

1. 扩展 `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py`，在每个历史决策点计算相同的入场时机字段。
2. 对比基线与新策略的 5/10/20 日收益、沪深300超额收益、胜率、MAE、MFE和样本保留率。
3. 分别统计 Spring/ST、首次 JAC、LPS/BU、再确认 JAC，不把不同结构混在一起。
4. 阈值调整必须只使用训练区间；保留最近一段交易日作为样本外验证。
5. 样本不足时维持保守阈值，不因少数成功追涨案例放宽门控。

### 阶段 G：安全上线

1. 先对最近正式快照做只读重放，确认奥士康、德福科技、双星新材等案例按预期被降级，同时正常的新鲜回踩信号仍保留。
2. 新门控直接约束“今日可执行”和“等待触发”；所有被过滤标的仍保留在观察池，避免候选发现断层。
3. 连续记录至少 10 个交易日的门控计数和后续表现，再决定是否调整 1.0/1.5 ATR、5%/8% 与年龄窗口。
4. 不通过自动演进流程放宽安全阈值；任何放宽都需要独立回测证据和人工审核。

## 验收标准

### 行为验收

- age > 3 的 LPS/BU 不得进入“今日可执行”或“等待触发”。
- age > 1 的首次 JAC 不得进入可执行层；首次 JAC 即使 age=0，也必须先等待回踩。
- 距触发价 > 1.5 ATR 或 > 8% 的候选不得进入可执行/等待层。
- 缺少触发价或 ATR 时不得默认视为新鲜。
- `retest_pending` 和 `failed_breakout` 仍保持硬否决。
- 被门控候选仍出现在观察池，并显示稳定、可审计的原因码。
- 买点奖励不能改变任何硬门控结果。

### 数据与兼容性验收

- 原有结构阶段、事件历史、`raw_composite_score` 和 `quality_adjusted_score` 保持兼容。
- 新字段进入 JSON、研究快照和 score ledger；同一次运行的 JSON/MD/HTML 值一致。
- 历史快照缺少新字段时明确显示 `unknown`，不能按 age=0 或 extension=0 处理。
- 盘中与收盘模式都使用各自冻结的决策时点价格，不读取未来 K 线。

### 效果验收

- 对 2026-09-10、09-11、09-14、09-15 快照重放后，可执行/等待层中 age≥5 的数量必须为 0。
- 可执行/等待层中超过 8% 或 1.5 ATR 的 JAC/LPS 数量必须为 0。
- 新鲜信号保留率、各结构样本量以及被拦截原因分布必须在回测报告中披露。
- 不把“候选数量下降”本身当作失败；主要观察假突破后的 MAE 是否下降、20日超额收益是否非劣化。

## 验证步骤

1. 运行针对性单元测试：
   - `test_wyckoff.py`
   - `test_daily_candidates.py`
   - `test_wyckoff_backtest.py`
2. 按项目要求运行两个强制质量门：
   - `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py`
   - `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff`
3. 对四份近期快照做确定性重放，核对门控原因和报告字段。
4. 运行维科夫历史回测，比较基线与修复版本的 5/10/20 日表现和 MAE/MFE。
5. 生成一份候选 HTML，人工核对表格排序、原因文案和 JSON/HTML 一致性；不自动打开浏览器。

## 风险与缓解

- **门控过严导致错过强趋势**：结构候选继续保留在观察池；先以 ATR 和百分比双指标审计，并通过分结构回测校准。
- **不同涨跌幅制度下固定百分比失真**：ATR 为主要尺度，百分比只承担绝对安全上限。
- **历史缓存价格口径不一致**：触发价、当前价和 ATR 必须来自同一复权口径、同一决策快照。
- **旧快照兼容性**：缺字段统一降级为 `unknown`，禁止默认为安全。
- **排名变化难以解释**：原始分不改，新门控和时机分独立记账。
- **样本不足时误调参数**：安全阈值保持保守；放宽必须满足分结构样本和样本外验证要求。

## 停止条件

完成上述行为、兼容性和质量门验证，并取得四份近期快照的确定性重放证据后，修复可视为实现完成。阈值是否进一步放宽属于后续研究，不阻塞安全修复上线。
