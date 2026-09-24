# 今日推荐与候选股工作流

## /today-recommendation [candidates 参数] [--dry-run] [--json] [--postprocess background|sync]

“今日推荐”的统一入口。用户使用该自然语言时，直接运行：

```bash
python3 .claude/skills/stock-trend/scripts/bridge/run_today.py --json
```

`--top`、`--min-candidates`、`--no-html` 等原 candidates 参数可直接传入。`--dry-run` 不联网、不写入，只输出计划。默认先返回候选报告，后处理由本次调用启动的独立后台任务继续执行；不安装后台定时器：

1. 刷新 `market_regime`，成功后运行 candidates；刷新失败时不得使用陈旧上下文继续扫描。统一入口在市场刷新完成后先生成带“观察列表待更新”占位的今日复盘 HTML，并通过进度输出立即给出该绝对路径；候选扫描继续使用同一次 `market_regime.json`，不等待 HTML 后处理。候选扫描进入成功或失败终态后，才启动独立后台任务替换该 HTML 的观察列表区块，确保候选扫描写入的同日板块成分快照已经可见，原链接保持不变；更新失败不影响候选 JSON/MD/HTML 或正式推荐结果。当前依据日且同日成分缺失时，观察任务可按需获取并验证该行业的完整东方财富成分；历史依据日严格只读，不能用当前成员关系回填。候选原有量化逻辑完成后，默认运行新闻影子层：优先取巨潮公告，再补充个股新闻；只接受推荐决策时点之前、近 14 个自然日且发布时间明确的证据。新闻分数限制为 `[-3,+1]`，重大正式风险可在影子结果中否决，正向消息不得跨越市场环境、数据质量、板块持续性、资金背离或维科夫硬门槛。首版只冻结证据、展示影子排序，不改变正式推荐。
2. 从已有权威交易日历按上海时区取得最近已知完成交易日；15:10 前用上一交易日评价历史。日历缺失时明确跳过依赖交易日的后处理；日历只覆盖历史区间时继续评价已知区间，并明确呈现 `workflow.calendar.coverage_end`，不得把未覆盖日期解释为休市。
3. 报告就绪后返回 `workflow.status=report_ready`，并给出 `workflow.postprocess.task_id`；HTML 观察列表更新另有 `workflow.review_html` 任务状态。候选报告路径在 `report_paths.html`，复盘路径在 `report_paths.daily_review_html`。后台研究按 `factor_ablation_daily → close → weekly → monitor → factor_ablation_evaluation` 执行。每日消融和成熟评价分别受 10 秒、20 秒限制，close/weekly/monitor 保留各自 300 秒预算。研究任务用 `--status <task_id> --json` 查询；复盘 HTML 更新任务状态保存在同一后台目录的 `review_html` 子目录。
4. 后台按评价日期所属 ISO 周执行 weekly；只有同周已有成功且 `input.research_snapshots > 0` 的有效任务记录才跳过。失败或没有研究样本的成功空跑，均允许同周再次尝试。
5. 使用真实交易日执行 monitor。接口或契约异常触发现有安全恢复时必须说明并通知；普通统计退化仅标记人工复核。
6. `factor_ablation_daily` 只读取同一次正式扫描的冻结研究快照，验证真实选择范围、六维分数逐项舍入、质量因子和买点奖励后，在同一资格层内分别移除一个维度重排。它只生成观察用影子 Top 1/3/5，不改变正式权重、门槛、报告或发布指针。结果写入 `.cache/stock-trend/evolution/factor_ablation/daily/<依据日>/`。
7. `factor_ablation_evaluation` 严格按 `(record_id, research_snapshot_sha256, evaluation_contract_id)` 关联前向结果；5 日仅作质量排查，20 日为主窗口，60 日作方向确认。样本未达到 20 个成熟日期和 100 个去重事件时返回 `continue_accumulating`，不触发调权或发布。旧快照缺真实选择范围时返回 `scope_unverified`。
8. 周度诊断按新闻分数档与风险级别统计成熟样本的 5/10/20 日结果、沪深300超额收益、胜率与 MAE。新闻层是否提升准确性必须由前向样本回答；证据不足时明确“继续积累”，不得凭单日案例转正。`--no-news` 仅用于诊断降级，`--news-file <JSON>` 可注入带发布时间和来源的可复现证据。

需要诊断或兼容旧的同步行为时，显式传 `--postprocess sync`。后台任务状态保存在 `.cache/stock-trend/evolution/background/<task_id>/`，包括冻结输入、状态、日志和最终结果；报告成功不代表后台研究已完成。

JSON 保留候选输出，并追加 `workflow` 阶段结果和 `notifications`。通知覆盖本次及跨调用检测到的策略版本/参数变化与 `invalid_pointer_fallback` 等安全回退；首次建立基线不报告变更。回复中醒目呈现通知，但不向外部渠道推送。publish 仍需显式人工审核，统一入口不得自动发布。不得自动打开 GUI 或浏览器。

**最终交付链接（必须执行）**：在“今日推荐”最终回复中，候选报告链接后必须紧跟同一次统一入口中 `market_regime.py` 生成的今日复盘 HTML 链接，使用绝对本地路径和清晰标签：

```markdown
报告：[HTML 候选报告](<absolute_candidates_html_path>)
今日复盘：[HTML 复盘报告](<absolute_daily_review_html_path>)
```

通过统一入口运行时，优先链接本次市场刷新实际生成且已验证存在的复盘 HTML。若当前请求未执行 `run_today.py`（例如仅解释或展示既有候选报告），则链接 `reports/lists/` 中最近生成、已验证存在的 `daily-review-*.html`，并标注“最近复盘”。候选报告仍需使用当前请求实际生成或明确指定的文件；找不到可验证的复盘 HTML 时，第二行改为“今日复盘：未生成（未找到可用复盘报告）”。

只需候选扫描时继续使用独立的 `/candidates`。

## /candidates [--top N] [--min-candidates N] [--min-score N] [--sectors BK...] [--json] [--no-html] [--style-shadow FILE] [--strategy-shadow FILE] [--memberships FILE]

每日候选股 — 以热点板块绝对热度门槛筛选板块 → 维科夫漏斗扫成分股(每板块 25 只,吸筹/拉升买点子阶段)→ 按“综合分 + 数据资格”扩池 → 分为今日可执行、等待触发、观察池。

**步骤**：

1. 运行(默认生成 HTML):
```bash
python3 .claude/skills/stock-trend/scripts/scans/daily_candidates.py [--top 30] [--min-candidates 20]
# 手动指定板块: --sectors BK0420,BK0897; Agent 消费: --json（默认同时生成 HTML）; 仅 MD: --no-html
```
2. 默认可生成 HTML 报告，但不自动打开；仅在用户明确要求查看时运行：
```bash
open -a "Google Chrome" reports/lists/candidates-<最新时间>.html
```
3. 每只候选附 `data_quality`：统一输出各维度 `data_date/fetched_at/source/quality/stale_reason`；K 线必须覆盖最近有效推荐依据日，总覆盖率必须 ≥70%，已返回的资金/基本面维度不得为错误状态。不满足者保留在观察池，并明确缺失或过期原因。报告将该指标标为“数据维度覆盖率”；候选表只展示小级别维科夫阶段、短线买点和短线置信度，不展示中线结构、周期结论、中线置信度或中期结构 K 线根数。今日推荐分桶不读取长短周期对齐结论，仍由短线买点、市场环境、数据质量和板块持续性共同决定，不以交易计划字段作为候选资格门槛。统一“今日推荐”入口另附 `news_analysis` 与 `news_shadow`，二者固定标注“实验观察，不参与推荐”。
4. 自动读取 `market_regime.json` 并执行硬门控：评分 `<60`、数据缺失或日期过期时仅输出观察池；`60–79` 最多 2 只等待触发；`≥80` 最多 5 只今日可执行。**盘中(交易时间内)保留上述市场环境档位和数量限制**，但所有结果标记 `provisional: true` + reason `intraday_provisional`，顶部报告保留“盘中临时(未收盘确认)”警告，行级诊断显示为“盘中临时状态”而非数据异常；盘中结果不写入正式推荐历史，收盘后需复跑 `/daily-review` + `/candidates` 确认最终结论。
5. 排序同时保留 `raw_composite_score`/兼容字段 `composite_score`，并新增 `quality_adjusted_score = raw × coverage_factor × freshness_factor`；扩池和最终排名使用质量调整分。候选资格继续使用 `quality_adjusted_score`；同一推荐层级内先检查数据质量、板块持续性、资金背离和短线状态，再使用 `execution_priority_score = min(100, quality_adjusted_score + buy_point_priority_bonus)` 排序并截取。严格一级/二级/三级奖励分别为 `+1/+3/+2`；普通 JAC、未确认或过期信号不奖励。买点优先分不能越过市场环境、数据质量、板块持续性、资金背离、`retest_pending`、`failed_breakout` 或最低质量分门槛。`evidence.status != ready` 时奖励属于保守先验，只允许通过后续回测缩小、归零或调整，不允许自动放大。
6. 热点板块同时保留绝对/相对热度，读取最近 3/5/10 日快照计算持续性和相对沪深300强弱；缺少至少两日持续性证据的单日脉冲只能进入观察池，手动指定但未经持续性验证的板块同样只观察。正式持续性快照只接受收盘后的东方财富 `push2` 行业+概念完整截面；AKShare 行业数据、BK 历史 K 线和旧 Top-30 记录只能作旁证，不能提升完整覆盖天数。同花顺排行如需使用东方财富成分，只能作为标注为“跨源未验证”的观察回退，不得继承排行资格；同类目录在一次扫描内共享加载，报告分别展示接口状态、映射失败和成分可用覆盖率。
7. 输出：今日结论 + 今日可执行/等待触发/观察池三层结果 → `reports/lists/candidates-<时间>.md` + `.html`。候选报告只呈现候选发现、维科夫分层、市场/板块资格和数据质量；不展示入场、止损、目标、R:R、仓位或有效期等交易计划字段，也不以交易计划完整性进行升降级。`--json` 保留原 `candidates` 字段供兼容消费，并新增 `policy`、三层推荐、`meta.tracking`。
   可选 `--style-shadow <FILE>` 加载五风格独立观察（沪深300/中证500/中证1000/创业板指/科创50）；必须通过 schema、模型、参数、基准日期和旧市场上下文指纹校验，失败时仅显示降级诊断。配合 `--memberships <FILE>` 提供含 `known_at`、生效区间和 `source` 的历史成分证据；没有成分证据的候选标为 unknown。该段固定标注“实验观察，不参与推荐”，只写报告副本，不写入正式推荐快照。影子运行另存于 `.cache/stock-trend/market_shadow_history/candidate_runs/`，样本范围为本次扫描候选，不代表全市场覆盖。
   可选 `--strategy-shadow <FILE>` 仅接受 P3 已冻结的买点奖励对照定义（严格等级 `+1/+3/+2` 对 `0/0/0`）；以同一次扫描输入独立重排并存入 `market_shadow_history/strategy_runs/`，不改变正式排序、分桶、快照或报告结论。
   P4 发布后的正式版本只从 `.cache/stock-trend/evolution/releases/active_policy.json` 原子指针读取；快照的 `policy.evolution_version` 与研究参数会记录实际版本。没有显式发布或指针异常时，自动使用原始 `+1/+3/+2` 基线。
8. 复核：候选仍需人工确认基本面和事件公告后再入场；弱市、盘中或证据不足时允许“今日无推荐”。单次运行在入口固定市场上下文，策略、快照、MD、HTML 和 JSON 必须使用同一市场依据；正式收盘结果按交易日写入 `.cache/stock-trend/recommendation_history/YYYY-MM-DD.json`，同内容重复运行幂等、不同内容冲突且不覆盖；保存失败只降低追踪状态，不抑制报告输出。P0 不代表完整生产链收益已经验证。
9. 无 Tushare 权限时，可用独立收盘采集命令积累板块完整历史，不依赖候选扫描：`python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --json`（15:10 后运行）；用 `--status --json` 检查本地覆盖，用 `--dry-run --json` 只验证不写入。首次上线通常需要 2–3 个交易日积累；失败日保留缺口，不用当前成分或 BK K 线伪造历史。

市场风格影子观察可独立运行：
```bash
python3 .claude/skills/stock-trend/scripts/analysis/market_style.py \
  --context .cache/stock-trend/market_regime.json --json --save
python3 .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
  --style-shadow .cache/stock-trend/market_shadow_history/formal/<YYYY-MM-DD>/<digest>.json \
  --memberships <historical-memberships.json>
```
五个风格固定为沪深300(`000300.SH`)、中证500(`000905.SH`)、中证1000(`000852.SH`)、创业板指(`399006.SZ`)和科创50(`000688.SH`)。影子模型只记录 MA20 上下方/斜率观察，运行结果、旧上下文指纹和原始输入独立留痕；不替代正式市场环境评分，也不改变候选推荐门槛。成分匹配必须使用当时已知(`known_at`)且覆盖基准日的历史区间，并带来源；缺证据显示 unknown。

推荐演进的高级 CLI（“今日推荐”日常调用使用上方统一入口；以下命令用于研究排障、实验回放和人工审核）：
```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py close --as-of YYYY-MM-DD --json
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py weekly --as-of YYYY-MM-DD --dry-run --json
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py monitor --json
# 仅人工审核且实验已处于 eligible 状态后：
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py publish --experiment-id <ID> --evidence release_evidence.json --json
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py rollback --evidence '监控发现策略异常，人工回滚' --json
```
publish 使用通过 `scripts/core/evolution_registry.py` 中 `verify_release_evidence` 验证的完整 `evolution-release-evidence/v2` 文件；任意摘要不能代替发布证据，引用结果必须从受控存储解析并验证。rollback 的 `--evidence` 则为纯文本 `summary`，不读取文件。

`close` 发现缺少正式上下文、板块快照或候选记录时保留缺口，下次可重试；`weekly` 即使未配置模型也会完成统计。统计退化只要求人工复核；monitor 遇到接口/契约异常时按现有机制安全恢复到已验证版本并通知，不能把普通统计退化当作自动恢复条件。

### 今日推荐质量契约

市场环境盘中混合只替换锚分与外推分，必须保留五项组件计算出的
`data_quality`、缺失/部分组件和归一化审计字段；同日旧盘中缓存缺字段时仅允许从冻结
组件在内存中补齐，不得回写。`completeness` 与 `freshness` 独立，时间戳未知只能显示
`freshness=unknown`/来源时间未记录，不能冒充 fresh 或组件缺失。

新闻影子风险按四级聚合 `critical > high > medium > none`：单独连续涨停或异常波动
为 medium；与风险提示、业务未开展、不存在相关业务等组合至少为 high，媒体证据需
官方公告核验；只有受信官方明确重大风险可 critical 并触发 shadow veto。否定/解除
表达保持不升级，新闻层始终不影响正式推荐。
