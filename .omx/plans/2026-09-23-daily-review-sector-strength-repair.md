# 今日复盘观察列表板块强度修复计划

## 需求摘要

修复 `daily-review-20260923-135812.html` 中 YAML 观察列表的“板块强度”缺失，并恢复
“综合 / 质量调整”分。修复必须同时解决数据时序、观察标的不在候选热点板块集合中、以及
板块分被单标的样本固定抬高的问题；不能只把 `null` 替换为默认分。

本计划只描述实施和验证，不修改业务代码、不刷新行情、不覆盖报告或分析 artifact。

## 已确认根因

1. `run_today.py` 在候选扫描开始前就启动复盘观察列表后台任务，见
   `.claude/skills/stock-trend/scripts/bridge/run_today.py:243-264`。2026-09-23 的观察分析
   artifact 在 13:58:13 生成，而部分同日板块成分快照在 13:58:18 以后才落盘，构成确定的
   读写竞态。
2. 观察输入只扫描 `.cache/stock-trend/sector_stocks/history/<data_date>/` 已有文件，缺少股票
   就直接返回“缺少本依据日证券元数据/行业成分快照”，见
   `.claude/skills/stock-trend/scripts/analysis/observation_list_analysis.py:211-271`。候选扫描只抓
   自己选择的板块，因此即使等待扫描结束，也不能保证覆盖 YAML 观察标的，例如联科科技。
3. 观察分析逐只调用 `run_phase2([candidate])`，见
   `.claude/skills/stock-trend/scripts/analysis/observation_list_analysis.py:435-450`。因此板块内
   相对排名的 cohort 只有自己，必然触发最高 10 分“板块内滞涨”奖励；同时原始排行缓存
   不含 `hot_score` 时回退到 50。两者叠加使有成员关系的观察标的通常得到固定 60 分，而不
   是真实板块强度。相关公式见
   `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:1914-1972`。
4. “综合 / 质量调整”必须在六维齐全时才展示；综合分使用含维科夫的六维权重，质量调整为
   `raw * coverage_factor * freshness_factor`，见
   `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:1975-1985` 和
   `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:2913-2918`。当前缺成员证据令
   `freshness_factor` 乘以 0.8，见同文件 `1995-2031`。

## 设计决策

采用“候选扫描终态后启动观察更新 + 当前依据日按需补齐行业成员证据 + 使用完整板块 cohort
评分”的组合方案。

- 不采用单纯增加后台延迟：延迟不能保证候选扫描完成，也无法覆盖非热点板块。
- 不采用给缺失板块默认 50/60 分：这会伪造证据并掩盖质量降级。
- 不采用为每次观察分析遍历全部行业/概念板块：请求量大，且概念多重归属会使主板块选择不
  稳定。
- 历史依据日保持严格只读：只有 `data_date` 等于当前可验证交易日时才允许按需联网补齐；
  历史报告只能使用当日已冻结快照，避免未来信息污染。

## 实施步骤

1. **先锁定竞态与评分回归。**
   在 `.claude/skills/stock-trend/tests/test_run_today.py` 增加失败用例：断言复盘链接仍在候选前
   产生，但观察更新任务只能在候选扫描进入 completed/failed 终态后启动。增加“候选扫描后
   快照才出现”的 fixture，证明后台分析能够看到同日快照。

2. **调整后台任务时序，不延迟初版报告。**
   在 `.claude/skills/stock-trend/scripts/bridge/run_today.py:243-278` 保留市场刷新后立即生成
   pending HTML 和输出链接，但把 `_launch_review_html_update()` 移到候选扫描的终态处理之后。
   候选成功与失败都允许启动观察更新；候选失败时观察任务独立按需补证，且不得改变候选或
   市场状态。

3. **为当前依据日增加按需行业成员解析。**
   在 `.claude/skills/stock-trend/scripts/analysis/observation_list_analysis.py` 将
   `build_candidates()` 改为两段式：先读同日历史快照；对仍缺失的代码，仅在当前依据日调用
   轻量行业解析。解析应从东方财富个股资料取得行业名称，与本次已冻结、完整的东方财富行业
   排行做唯一精确匹配，再调用现有 `fetchers/sector_data.py` 成分接口验证目标代码确实属于该
   板块。验证通过后按现有 schema 原子写入同日成分快照，并标记
   `membership_quality=same_day_verified`；名称映射不唯一、目标代码不在成分中或写缓存失败时
   继续返回缺失，不得给分。概念板块不作为首版主行业回退。

4. **恢复真实板块热度与完整 peer cohort。**
   在读取同日排行时复用
   `.claude/skills/stock-trend/scripts/fetchers/sector_data.py:319-405` 的
   `compute_hot_score/rank_hot_sectors`，不得把缺失 `hot_score` 静默当作 50。候选输入同时携带
   该板块完整成分股的当日涨跌幅 cohort；观察模式调用 `run_phase2` 时显式传入该 cohort，或
   抽出一个纯函数直接计算成员分。保留逐标的失败隔离，但禁止用 `[candidate]` 自身构造板块
   分位。若成分快照只是 Top-N 而非完整板块，板块强度保持 unavailable，并给出
   `sector_peer_coverage_incomplete`，不能冒充完整排名。

5. **重算六维、质量状态与报告行。**
   成员证据为同日 verified 后，`apply_membership_quality()` 不再添加
   `sector_membership_stale`，基础数据本身完整时 `coverage_factor=1.0`、
   `freshness_factor=1.0`。继续复用 `composite_from_dimensions()`，由 artifact 写出
   `sector_strength`、`raw_composite_score`、`quality_adjusted_score` 和成员证据；
   `.claude/skills/stock-trend/scripts/analysis/market_regime.py` 只负责展示，不自行补分。

6. **增加针对性测试与契约测试。**
   扩展 `.claude/skills/stock-trend/tests/test_observation_list_analysis.py`：
   - 同日已有快照直接命中且不联网；
   - 当前依据日缺失时按需解析、唯一行业匹配、成员验证和原子缓存；
   - 历史依据日禁止联网补齐；
   - 非热点行业仍能取得同日成员和板块分，但不获得 `sector_actionable`；
   - Top-N 成分快照不能充当完整 peer cohort；
   - 两只同板块、不同涨幅的观察标的得到不同相对排名分；
   - 单标的分析不再固定获得 10 分滞涨奖励；
   - 无效五位代码继续显示错误且不参与综合分。
   同时扩展 `.claude/skills/stock-trend/tests/test_stock_scanner.py`，锁定热度归一化、cohort 注入、
   六维综合权重及质量因子。

7. **更新工作流说明并验证。**
   更新 `.claude/skills/stock-trend/SKILL.md` 的 `/today-recommendation` 与 `/daily-review`：初版
   链接仍立即交付，观察分析在候选终态后运行；当前日可做同源按需成员补证，历史日严格只读。
   先跑目标测试，再执行两个强制质量门：

   ```bash
   /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py
   /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff
   ```

   不为消除失败而重生成 golden；只有确认 HTML 契约变化符合预期后才更新快照。

## 当前报告的可复算结果

以下只使用 2026-09-23 artifact 已冻结的五个非板块维度，并按**现有代码的机械回填行为**假设
`sector_strength=60.0`。这是用来证明综合分链路可恢复的诊断值，不是修复后真实板块评分：

| 股票 | 动量 | 量价 | 资金 | 基本面 | 假设板块 | 维科夫 | 综合 | 质量调整 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 思泉新材 301489 | 95.0 | 45.0 | 40.0 | 35.0 | 60.0 | 83.3333 | 66.8 | 66.8 |
| 联科科技 001207 | 95.0 | 65.0 | 40.0 | 35.0 | 60.0 | 83.3333 | 69.8 | 69.8 |
| 赢合科技 300457 | 90.0 | 50.0 | 65.0 | 35.0 | 60.0 | 83.3333 | 70.1 | 70.1 |

公式为：

`综合 = 动量×0.25 + 量价×0.15 + 资金×0.15 + 基本面×0.10 + 板块×0.10 + 维科夫×0.25`

同日 verified 成员证据成立后，三只股票现有基础数据的覆盖率和新鲜度均为 1.0，因此机械
回填场景下 `质量调整 = 综合 × 1.0 × 1.0`。若仍沿用当前 stale 质量惩罚，则对应值仅为
53.4、55.8、56.1，但当前 renderer 因六维不完整而选择显示 `—`。

现有同日缓存只能对思泉新材和赢合科技做不完整的诊断估算：使用已归一化行业热度和现存
Top-N cohort 时，板块分约为 52.29、54.19，对应综合分约为 66.1、69.5。因为思泉新材所在
板块快照只有 25 只而行业涨跌家数为 37，且联科科技没有 2026-09-23 同日成分快照，这些值
不能作为最终修复结果。最终值必须在完整同日 cohort 验证后生成。

## 验收标准

1. 初版复盘 HTML 仍在候选扫描前生成并输出路径；观察更新任务的启动时间严格晚于候选扫描
   终态。
2. 当前交易日 YAML 中每个有效 A 股代码要么取得同源、同日、已验证的行业成员关系和完整
   cohort，要么明确显示缺失原因；绝不使用默认 50/60 冒充板块分。
3. 板块强度可追溯到排行日期、板块代码、热度分、成分日期、成分覆盖数和个股板块内分位。
4. 同一板块内涨幅不同的两只股票不因逐只分析而都获得最高滞涨奖励。
5. 六维齐全时综合分严格等于共享权重函数结果；质量调整严格等于
   `round(raw * coverage_factor * freshness_factor, 1)`。
6. 历史依据日缺快照时不联网、不借用当前成员关系，报告继续降级而不是回填未来数据。
7. 目标测试、`test_stock_trend.py` 和 `test_golden.py --diff` 全部通过；没有未经确认的 golden
   更新。

## 风险与缓解

- **行业名称口径不一致：** 只接受东方财富到东方财富的唯一精确匹配并二次验证目标成分；
  任何模糊或跨源匹配均降级。
- **成分接口只返回 Top-N：** 为观察行业请求完整分页/足够 `pz`，并以排行中的涨跌家数校验
  覆盖；不足时不计算相对分位。
- **后台变慢：** 先复用候选快照，仅对缺失观察代码按唯一行业去重后请求；设置现有源健康、
  超时和请求上限，失败不阻断正式推荐。
- **盘中数据变化：** artifact、排行和成分快照必须绑定同一 `data_date` 与采集批次；报告继续
  标记盘中临时，收盘后复跑确认。

## 停止条件

修复完成的判据不是“列里出现数字”，而是所有有效观察标的的板块分都有完整同日证据，综合
与质量调整可由 artifact 独立复算，历史隔离与候选推荐路径不回归，并通过两项仓库质量门。

## 执行结果（2026-09-23）

已按本计划实施并用当日同源成分接口补齐 2026-09-23 快照。最终 artifact 与原 HTML 观察区块
均已更新：

| 股票 | 板块强度 | 综合 | 质量调整 | 状态 |
| --- | ---: | ---: | ---: | --- |
| 思泉新材 301489 | 56.2 | 66.5 | 66.5 | 因维科夫买点未确认而降级 |
| 联科科技 001207 | 52.0 | 69.0 | 69.0 | 因维科夫买点未确认而降级 |
| 赢合科技 300457 | 55.4 | 69.6 | 69.6 | 因维科夫买点未确认而降级 |
| 宏辉果蔬 603336 | 58.5 | 73.2 | 73.2 | 因维科夫买点未确认而降级 |

验证结果：观察分析 5 项、后台时序 36 项、扫描器 110 项、市场复盘 123 项、主质量套件
142 项均通过；golden diff 为 21 passed、0 failed、2 warnings。`test_daily_candidates.py`
仍因仓库 HEAD 与测试文件既有不一致而无法导入 `merge_sector_resonance`，主质量套件按既有
逻辑将该组标为 skipped，未因本次改动隐藏新的失败。
