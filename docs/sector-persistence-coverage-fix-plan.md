# 候选板块持续性历史覆盖修复计划

## Requirements Summary

修复 `/candidates` 将“板块未进入历史 Top 30”误判为“历史数据不足”的问题，同时保持市场主线、周报和推荐安全门不被放松。

当前 `daily_candidates.enrich_sector_context()` 使用板块在 Top-30 快照中的实际出现次数作为 `persistence_days`，并以 `days < 3` 生成 `history_insufficient`。这混合了两个不同概念：

- 历史覆盖：当天是否存在可信、完整的板块排行快照。
- 热点出现：目标板块当天是否达到热点门槛或进入热榜。

现有 `sector_snapshot_history.json` 不能直接扩成全市场快照，因为 `market_theme.py` 和 `weekly_report.py` 将“是否存在于该文件”用于计算上榜率/频率。直接扩容会令所有板块近似每天上榜，改变既有评分语义。

## Recommended Design

采用双轨快照，并保持现有文件兼容：

1. 保留 `sector_snapshot_history.json` 的 Top-30 语义，继续服务 `/market-theme` 和 `/weekly`。
2. 新增候选专用 `candidate_sector_history.json`，按交易日保存候选筛选口径下的完整板块排行。
3. 将持续性字段拆分为：
   - `history_coverage_days`：窗口内完整、日期有效的排行快照天数。
   - `sector_observed_days`：窗口内可以判定该板块状态的天数。
   - `hot_appearance_days`：达到候选热点门槛的天数。
   - `hot_streak`：截至推荐日连续达到热点门槛的天数。
   - 保留 `persistence_days` 作为 `hot_appearance_days` 的兼容别名。
4. `history_insufficient` 只表示覆盖不足，不再表示板块没有反复上榜：
   - `history_coverage_days < 2`：`history_insufficient`。
   - 覆盖至少 2 天、但只有当前日达标：`single_day_pulse`。
   - 最近 2 个有效交易日连续达标且持续性分达到 45：`emerging`。
   - 最近 3 个有效交易日连续达标、持续性分达到 60、相对沪深300不弱：`mainline`。
5. 不从旧 Top-30 文件推断“未出现即低热度”。旧记录只提供正向上榜证据；新全量快照积累不足时保持观察，避免错误晋级。

## Acceptance Criteria

1. 存在 10 个完整排行快照、某板块只在推荐日达到热点门槛时，结果必须是 `single_day_pulse`，不得是 `history_insufficient`，且 `sector_actionable=false`。
2. 只有 0 或 1 个完整快照日时，结果必须是 `history_insufficient`。
3. 最近 2 个有效交易日连续达标且持续性分不低于 45 时，可判定 `emerging`；最近 3 日连续达标、持续性分不低于 60且相对强弱非负时，可判定 `mainline`。
4. 快照缺失、日期不匹配、排行不完整或来源质量非 `good` 的日期不得计入 `history_coverage_days`。
5. 当前排名第 31 位以后、但达到候选绝对热度门槛的板块，能够在新快照中保存并参与后续 3/5/10 日计算。
6. `market_theme.py` 和 `weekly_report.py` 继续读取原 Top-30 快照；其现有上榜率、周频率和 golden 输出不因本改动变化。
7. 报告和 JSON 至少显示“覆盖天数、热点出现天数、连续热点天数”，用户可以区分冷却、单日脉冲和数据缺失。
8. 新历史保留最近 30 个有效交易日；按当前约 996 个板块估算，缓存文件应控制在 10 MB 以内。

## Implementation Steps

### 1. 增加候选专用全量快照

修改 `.claude/skills/stock-trend/scripts/fetchers/sector_data.py`：

- 保留当前 `SNAPSHOT_FILE`、`_hot_ranked_sectors(..., top_n=30)` 和 `load_snapshot_history()` 不变。
- 新增 `CANDIDATE_SNAPSHOT_FILE`、`append_candidate_sector_snapshot()` 和 `load_candidate_sector_history()`。
- 新快照保存 `schema_version` 和按日期索引的记录；每个日期记录 `complete`、`data_date`、`universe_count`、筛选口径及 compact sector rows。
- compact row 至少包含 `code/name/rank/absolute_hot_score/relative_hot_score/change_pct/net_flow/up_ratio`。
- 只接受 `meta.complete is True`、有效交易日且日期匹配的排行；写入采用临时文件加 `os.replace()`，并保留最近 30 个有效日期。
- 新文件保存候选筛选后的完整 ranked universe，不使用 Top-30 截断。

依据：现有 Top-30 截断位于 `sector_data.py:933` 和 `sector_data.py:988`；现有读取契约位于 `sector_data.py:1026`。

### 2. 在候选扫描中写入并读取同一份排行证据

修改 `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`：

- 在 `rank_hot_sectors(..., top_n=None)` 产生本次不可变排行后，写入候选专用快照；确保写入数据与实际候选筛选使用同一排行、过滤和排序结果。
- 继续调用原 `append_daily_snapshot()` 保存 Top-30 市场主线快照。
- 将新历史传入 `enrich_sector_context()`；写入失败只记录 degradation，不把不完整日期当作历史覆盖。
- 当前实时排行是推荐日的直接证据，不应要求“先写文件、再读文件”才能成为 `latest_present`；以 `ranking_data_date == as_of_date` 且来源质量有效作为当前日依据。

依据：当前候选排行和绝对热度筛选位于 `daily_candidates.py:970`、`daily_candidates.py:978`，历史加载位于 `daily_candidates.py:1007`。

### 3. 拆分覆盖、出现和连续性语义

重构 `daily_candidates.enrich_sector_context()`：

- 用完整快照日期计算 `history_coverage_days`，不再用板块出现次数判断数据是否存在。
- 对每个完整日期生成明确 observation：达到 `min_hot` 为 hot；存在但未达标为 cold；快照不完整为 unknown。
- 3/5/10 日均值只使用 known observation；unknown 不按 0 分处理，完整日的 cold 可以按实际分数或 0 分下限处理。
- 使用独立常量，例如 `MIN_PERSISTENCE_COVERAGE_DAYS=2`、`EMERGING_STREAK_DAYS=2`、`MAINLINE_STREAK_DAYS=3`，避免再次把业务阈值混为一个数字。
- `persistence_status` 的优先级固定为：覆盖不足 → 已验证 mainline/emerging → single_day_pulse。
- 保持 `sector_actionable` 只允许 `mainline` 或 `emerging`，不因标签修复扩大推荐集合。

依据：当前语义混合发生在 `daily_candidates.py:620`、`daily_candidates.py:636` 和 `daily_candidates.py:712`。

### 4. 传播证据字段并改进报告解释

修改：

- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`

将新增覆盖字段作为 additive fields 写入 sector membership、候选 JSON 和报告。报告建议显示：

`持续性：快照覆盖 10/10｜热点出现 1/10｜连续 1 日｜单日脉冲`

`REASON_LABELS` 保留 `history_insufficient`，但只用于真实覆盖不足；重复上榜不足继续使用 `single_day_pulse`。

依据：membership 证据构建位于 `stock_scanner.py:140`，报告原因组装位于 `daily_candidates.py:1364`，观察池降级位于 `daily_candidates.py:1763`。

### 5. 测试先行并锁住下游兼容性

修改 `.claude/skills/stock-trend/tests/test_daily_candidates.py`，增加：

- 10 日完整覆盖、仅 1 日热点 → pulse，不是 history insufficient。
- 1 日覆盖 → history insufficient。
- 2 日连续热点 → emerging。
- 3 日连续热点 → mainline。
- 完整日未达热点与 partial/unknown 日的区别。
- 排名 31–56 的板块能够写入并在下一交易日读取。
- 写入失败、日期不匹配、partial ranking 不增加覆盖天数。
- 旧 Top-30 快照只提供正向证据，不从缺席记录推断冷却。

补充 `test_market_theme.py` 和 `test_weekly_report.py` 回归断言，证明原 Top-30 文件和上榜频率语义未改变。

## Risks and Mitigations

- 风险：直接扩展原快照会令市场主线/周报认为所有板块每天上榜。缓解：使用候选专用第二份历史，不修改原 Top-30 契约。
- 风险：把缺席直接记为 0 会将 partial 数据误判为退潮。缓解：只有 `complete=true` 的新快照允许形成 cold observation；partial 为 unknown。
- 风险：上线后新历史需要时间积累。缓解：旧 Top-30 的实际出现可作为正向证据，但缺席不作负向推断；完整判定最多等待 2–3 个交易日。
- 风险：推荐集合意外扩大。缓解：标签修复不改变 `sector_actionable`；只有满足既有 emerging/mainline 连续性、分数和相对强弱门槛才可晋级。
- 风险：并发写入导致快照损坏。缓解：原子替换、同日期幂等覆盖、有效日期剪枝。

## Verification Steps

按顺序运行：

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_market_theme.py
python3 .claude/skills/stock-trend/tests/test_weekly_report.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

使用固定 fixture 生成 56 个候选板块、其中至少一个排名大于 30，验证第二天和第三天的状态转换：

`history_insufficient → single_day_pulse/emerging → mainline（仅在满足门槛时）`

停止条件：所有定向测试和两个仓库质量门通过，原 Top-30 快照消费者输出未变化，新报告不再将“未反复上榜”表述为“没有历史数据”。
