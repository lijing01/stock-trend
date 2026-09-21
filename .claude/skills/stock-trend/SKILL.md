---
name: stock-trend
description: 分析 A股、港股和 ETF 的中线趋势并生成结构化报告；也用于今日推荐、ETF 扫描、观察列表、ETF/维科夫回测、每日复盘及候选股。用户提到股票或 ETF 趋势、代码分析、观察列表、选股、复盘或上述工作流时使用。
---

# 股票趋势判断

使用 `$stock-trend` 显式调用；也可直接用自然语言说明标的或工作流。下文保留的 `/stock-trend`、`/etf-scan` 等名称是工作流路由标识，不要求 Codex 提供同名 slash command。

运行 Python 脚本时以仓库根目录为工作目录。行情和新闻属于时效性数据，必须使用可用的联网工具或脚本实时获取；网络受限时申请授权，若仍不可用则明确标注降级或数据缺失，不得把旧缓存冒充实时数据。仅在用户明确要求时打开 GUI 或浏览器。

Python 运行要求为 >=3.10；下文 `python3` 指满足要求的解释器。本环境默认 `python3` 为 3.9.6，Agent 必须将命令中的解释器替换为已安装的 `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3`，无需修改全局环境。

## 东方财富 / 同花顺实时接口运行契约

东方财富和同花顺的实时行情接口在受限沙盒中可能出现 DNS 失败。凡是运行会访问这些来源的 fetcher 或工作流（包括 `market_regime.py`、`daily_candidates.py`、行业扫描，以及 K 线、资金流 fetcher），Agent 必须直接在**沙盒外**执行，且仅对该次命令设置 `NO_PROXY` 和 `no_proxy`：

```bash
NO_PROXY="${NO_PROXY:+${NO_PROXY},}eastmoney.com,.eastmoney.com,10jqka.com.cn,.10jqka.com.cn" \\
no_proxy="${NO_PROXY:+${NO_PROXY},}eastmoney.com,.eastmoney.com,10jqka.com.cn,.10jqka.com.cn" \\
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 <script> [args]
```

- 对 Codex 工具调用，这意味着使用 `require_escalated` 启动该命令；不要先在沙盒内重试这些实时接口。
- 仅追加本次进程的代理绕过名单，不修改 shell profile、全局代理或系统 DNS 设置。`eastmoney.com` / `.eastmoney.com` 覆盖东方财富子域，`10jqka.com.cn` / `.10jqka.com.cn` 覆盖同花顺子域。
- 外部直连仍失败时，记录失败来源与原因，按既有缓存/降级规则继续；报告必须标注 `degraded`、`cached` 或数据缺失，绝不能称为实时数据。

**分支路由**：用户说“今日推荐”→`/today-recommendation` 统一入口；`/candidates`→仅候选扫描；`/etf-scan`→ETF扫描；`/etf-backtest`→回测；`/stock-trend`→下方Step 1-4。除“今日推荐”的统一流程外，各流程独立。

---

## /etf-scan [--top N] [--focus <板块>] [--output compact|full]

扫描精选ETF池，输出趋势排名。`--focus`: 宽基指数/科技/金融/消费医药/制造周期/商品跨境。

**步骤**：

1. 运行扫描：
```bash
python3 .claude/skills/stock-trend/scripts/scans/etf_scanner.py [--top N] [--focus <板块>] [--output compact|full] --output-html
```
输出JSON(stdout)含 `meta`/`combined_ranking`/`top_picks`/`excluded`/`sector_summary`。`--output-html` 生成 `reports/lists/YYYY-MM-DD-HH-mm.html`。

2. 呈现：完整模式→排名表+Top3逻辑+排除+板块强弱。`--compact`→Top5表+Top1-2逻辑+排除摘要。

3. 信号映射：≥+2.0→↑↑看多，+0.5~+2.0→↑偏多，-0.5~+0.5→→震荡，<-0.5→↓偏空。星级：≥80→★★★，≥65→★★☆，≥50→★☆☆。`deep_score`=null时标注"深度分析跳过"。必须附带免责声明。

---

## /etf-backtest [--lookback-days N] [--focus <板块>] [--top-n N] [--eval-windows 5,10,20] [--etf <代码>]

回测ETF Phase 1速评分模型预测力。默认120天/top10/窗口5,10,20/间隔5。

**步骤**：

1. 运行：
```bash
python3 .claude/skills/stock-trend/scripts/backtesting/engine.py [--lookback-days N] [--focus <板块>] [--top-n N] [--eval-windows 5,10,20]
```

2. 呈现：回测区间→IC表(窗口/均值/标准差/t值/正向比)→命中率、Top vs Bottom收益差、Top10平均收益。IC>0.05且5%显著=有预测力；命中率>55%=优于随机。

---

## /wyckoff-backtest [--codes 600519,000001] [--sectors BK0477,...] [--from-candidates <json>] [--lookback-days N] [--eval-windows 5,10,20] [--min-confidence N] [--min-gap N] [--output <path>] [--output-html]

维科夫买点回测 — 验证 `/candidates` 买点信号的历史胜率。历史重放：每采样日用截至当日 K 线跑维科夫分析，检测买点（吸筹/拉升阶段 + Spring/LPS/ST/PRE_MARKUP/JAC/BU 子阶段 + 置信度≥阈值），测 5/10/20 日前向收益，对照全样本基线；同时严格区分一级 Spring/Test、二级 SOS 后 LPS、三级 JAC/BU 后再确认与未分级信号，输出各等级收益、MAE/MFE、样本数和证据状态。

**步骤**：

1. 宇宙来源三选一：
```bash
# 用候选报告验证 (生产路径): /candidates --json > candidates.json
python3 .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py --from-candidates candidates.json --output-html
# 手动给代码
python3 .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py --codes 600519,000001
# 复用 /candidates 热点板块宇宙
python3 .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py --sectors BK0477,BK0897
```

2. 默认 120 天 / 窗口 5,10,20 / 采样间隔 5 / 置信度≥0.3(与漏斗一致) / 同标的信号去重间隔 10 天。

3. 输出 JSON(stdout)：`summary`(信号vs基线 胜率/均收益/α)、`by_sub_phase`/`by_buy_level`/`by_confidence`/`by_phase`/`by_score_100`(分桶胜率)、`risk_by_buy_level`(各等级 MAE/MFE)、`evidence`(各等级样本数及是否达到最低证据门槛)、`health_audit`(确认事件健康门排除计数、状态/事件类型分桶及审计样本)、`ic`(置信度/100分→前向收益)、`strategy_stats`(主窗口,供凯利)、`signals` 明细。`--output-html` 生成 `reports/lists/wyckoff-backtest-*.html`。失效 JAC/Spring 虽保留 `confirmed_event` 与 `event_health` 供审计，但不进入 `signals`、`signal_pairs`、收益统计或买点奖励。

4. 判读：信号胜率显著高于基线=买点有 edge；某子阶段/置信度档位胜率突出=漏斗参数可据此收紧。`evidence.status != ready` 时，分级奖励只视为保守先验，不得据此放大；至少积累每级 100 个信号后，才可据 5/10/20 日收益与 MAE/MFE 调整或归零奖励。

**局限**：close-to-close 无成本无止损模拟；宇宙=当前成分股(幸存者偏差)；买点稀缺时样本少，需用完整 `/candidates` 宇宙跑，单看几只意义不大。

---

## /stock-scanner [--sectors BK0420,...] [--top N] [--min-score N] [--wyckoff]

A股热点板块成分股筛选器。三阶段：汇聚硬过滤(非A股/ST/市值50-2000亿)→多维打分(动量/量价/资金/基本面/板块强度)→排序定星。

**步骤**：

1. 直接给板块代码：
```bash
python3 .claude/skills/stock-trend/scripts/scans/stock_scanner.py --sectors BK0420,BK0897 --top 10
```

2. **`--wyckoff`(P0-2 选股漏斗)**：只保留维科夫**吸筹/拉升**阶段且子阶段为买点(Spring/LPS/ST/PRE_MARKUP/JAC/BU)、置信度≥0.3 的候选；不足 60 根 K 线的候选丢弃。新增 `wyckoff` 100 分维度，复合分重配为 动量0.25/量价0.15/资金0.15/基本面0.10/板块0.10/wyckoff0.25。输出含 `wyckoff` 字段(阶段/子阶段/置信度/研判)。

3. 呈现：Top 排名表(综合分/维度/信号/预警)。`--wyckoff` 时标注维科夫子阶段与置信度。

---

## /today-recommendation [candidates 参数] [--dry-run] [--json] [--postprocess background|sync]

“今日推荐”的统一入口。用户使用该自然语言时，直接运行：

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/scripts/bridge/run_today.py --json
```

`--top`、`--min-candidates`、`--no-html` 等原 candidates 参数可直接传入。`--dry-run` 不联网、不写入，只输出计划。默认先返回候选报告，后处理由本次调用启动的独立后台任务继续执行；不安装后台定时器：

1. 刷新 `market_regime`，成功后运行 candidates；刷新失败时不得使用陈旧上下文继续扫描。统一入口在市场刷新完成后先生成带“观察列表待更新”占位的今日复盘 HTML，并通过进度输出立即给出该绝对路径；候选扫描继续使用同一次 `market_regime.json`，不等待 HTML 后处理。候选扫描结束后独立后台任务只替换该 HTML 的观察列表区块，原链接保持不变；更新失败不影响候选 JSON/MD/HTML 或正式推荐结果。候选原有量化逻辑完成后，默认运行新闻影子层：优先取巨潮公告，再补充个股新闻；只接受推荐决策时点之前、近 14 个自然日且发布时间明确的证据。新闻分数限制为 `[-3,+1]`，重大正式风险可在影子结果中否决，正向消息不得跨越市场环境、数据质量、板块持续性、资金背离或维科夫硬门槛。首版只冻结证据、展示影子排序，不改变正式推荐。
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

---

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

---

### 今日推荐质量契约

市场环境盘中混合只替换锚分与外推分，必须保留五项组件计算出的
`data_quality`、缺失/部分组件和归一化审计字段；同日旧盘中缓存缺字段时仅允许从冻结
组件在内存中补齐，不得回写。`completeness` 与 `freshness` 独立，时间戳未知只能显示
`freshness=unknown`/来源时间未记录，不能冒充 fresh 或组件缺失。

新闻影子风险按四级聚合 `critical > high > medium > none`：单独连续涨停或异常波动
为 medium；与风险提示、业务未开展、不存在相关业务等组合至少为 high，媒体证据需
官方公告核验；只有受信官方明确重大风险可 critical 并触发 shadow veto。否定/解除
表达保持不升级，新闻层始终不影响正式推荐。

## /daily-review [--no-refresh] [--json] [--no-html]

今日复盘 + 市场环境评分 — 整合全市场上下文(大盘/成交额/涨跌家数/涨停/资金/板块排行),输出 0-100 市场环境评分 + 每日复盘报告,并持久化 `market_regime.json` 上下文供 `/stock-trend` 做大盘/板块对比。

**评分公式**: 大盘趋势(25%) + 成交额(20%) + 赚钱效应(25%) + 涨停情绪(20%) + 资金(10%)。≥80 强势(可建仓)/ 60-79 中性(轻仓观察) / <60 弱势(降仓/空仓,不找牛股)。

**评分解释与数据资格**: 正式大盘趋势组件当前使用上证(`000001.SH`)、沪深300(`000300.SH`)和深成指(`399001.SZ`)的 MA20 状态平均；中证500、 中证1000、创业板指和科创50不自动计入该正式分，除非后续版本明确变更模型。成交额组件另用上证与深证综指(`399106.SZ`)拼接两市成交额。每次上下文同时保存 `market_explanation/v1`：五项分数、权重、归一化分母、贡献、指数名单及证据资格。`data_quality=partial` 是正式推荐硬门控，不能因为分数可计算就放行。

证据资格与分数状态分开记录：完整/部分/缺失、fresh/stale/unknown、primary/alternative/estimate/unknown、scorable/reference_only/unavailable。市场资金正式使用全市场主力净流入(`market_main_force_net_inflow`)；该数据有效时标记 `primary`/`scorable`，不因北向数据不可用产生 `partial` 或 `regime_data_partial`。其他组件仍按各自完整性标记，缺少来源时间戳不得标记为 fresh。盘中解释额外保存锚分、外推分和混合权重；无法取得锚证据时只显示已存正式分，不声称组件合计解释了盘中混合分。

**盘中混合口径**: 交易时间内跑，评分为「上一收盘锚 + 盘中按已过 240 交易分钟占比外推」的混合 —— 半日成交额/涨停/涨跌家数按已过时间占比放大估全天值再打分，早盘(开盘约 40 分钟内)不外推、直接用上一收盘。越早越依赖昨收，因此**不会因半日数据误报弱势**。报告标 `盘中临时`，输出含 `intraday: true` + `intraday_note`；盘中快照**不写** `market_regime_history.json`(避免 partial 数据污染后续基线)。

**步骤**：

1. 运行(默认生成 HTML,`--no-html` 仅 MD,`--json` stdout 结构化输出):
```bash
python3 .claude/skills/stock-trend/scripts/analysis/market_regime.py [--no-refresh]
```
2. **默认打开 HTML 报告**:
```bash
open -a "Google Chrome" reports/lists/daily-review-<最新时间>.html
```
3. 数据源: 指数K线(东财→BaoStock降级)、行业板块排行、涨停池(AKShare)、地域板块涨跌家数/全市场主力净流入。
4. 输出: 复盘报告(①市场环境 ②板块最强/最弱) → `reports/lists/daily-review-<时间>.md` + `.html`；HTML 另含 ③“观察列表”。该区块读取同一依据日的候选扫描冻结观察池，按候选报告的十列展示，并保留新闻影子分与观察分级；统一入口先呈现“候选扫描进行中”，候选完成后后台原子替换同一报告链接。独立复盘若无同日完整候选副产物，明确显示不可用，不回退到其他日期。`observation_list.yaml` 仍为手工观察配置，但不作为此候选表数据源；缺失候选池只降级该 HTML 区块，不阻断市场评分。
5. 持久化: `market_regime.json`(今日上下文,供 /stock-trend 对比)、`market_regime_history.json`(30天,支撑涨停/成交额均值)。`--no-refresh` 仅重用市场缓存并直接读取观察列表，不写回观察列表配置。

复盘回复只呈现同一次运行的市场评分、宽度/情绪、板块强弱及数据质量说明；盘中或非当日结果必须标注 `intraday_note`/`stale_note`，缺失字段写“未提供/数据缺失”。完整报告保留 Markdown/HTML 路径，并附带：**本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。**

**与 /stock-trend 联动**: 每次先跑 `/daily-review` 生成市场上下文,`/stock-trend` 报告自动含「📊 大盘/板块对比」段(个股 vs 沪深300 相对强弱 + 所属板块位置)。

---

# /stock-trend 流程 (Step 1-4)

## Step 1: 解析输入

```
/stock-trend <code> [--focus technical|capital_flow|fundamental|sentiment] [--horizon intraday|daily|weekly] [--multi-timeframe] [--compact] [--no-data]
```

- `code`(必填)：股票/ETF代码或名称。`--focus`可叠加。`--horizon`默认daily。`--multi-timeframe`同日获取日/周K线计算周期共振。`--compact`精简输出。`--no-data`跳过K线获取。

**代码解析**：
```bash
python3 .claude/skills/stock-trend/scripts/core/resolve_code.py <name_or_code> -o /tmp/resolve.json
```
支持：6位A股/5位港股/带后缀(600519.SH)/中文名称(恒生科技ETF大成/茅台)。输出 `ts_code`/`asset`/`adj`/`market`/`name`。

## Step 2: 数据管线 + 四维搜索（并发）

### A. 数据管线

```bash
python3 .claude/skills/stock-trend/scripts/pipeline/runner.py --code <code>
```

内部自动：代码解析→K线(Tushare→东方财富降级)→技术分析→ETF数据(ETF标的)→资金流向(北向/融资/龙虎榜)→基本面(AKShare,ETF跳过)→宏观快照(汇率/利率/PMI/CPI/M2)→期货数据(ETF标的:基差/OI/量)。

输出到 `.cache/stock-trend/{code}/`：`pipeline_output.json`/`kline.json`/`technical.json`/`etf_data.json`/`futures_data.json`/`capital_flow.json`/`fundamental.json`/`macro_snapshot.json`/`wyckoff.json`。

**缓存TTL**：盘中5min/盘后16h(宏观:盘中4h/盘后12h,基本面:盘中30min/盘后16h)。`--no-cache`强制刷新。**超时**：每步30s，超时维度标记timeout。

可选K线图：
```bash
python3 .claude/skills/stock-trend/scripts/reporting/chart.py .cache/stock-trend/{code}/kline.json --technical .cache/stock-trend/{code}/technical.json --chip-distribution .cache/stock-trend/{code}/chip_distribution.json -o .cache/stock-trend/{code}/chart_fragment.html
```

### B. 四维并行搜索

管线启动后**立即**四维并行搜索（一次调用多个搜索工具）：

| 维度 | 权重 | 自动化基线 | 搜索词 | 反向验证词 |
|------|------|-----------|--------|-----------|
| 资金面 | 25% | `capital_flow.json`(北向/融资)+`futures_data.json`(基差/OI) | `"{name} {code} 资金流向 北向资金 {YYYY}年{M}月"` | `"流出 危机 减持"` |
| 基本面 | 15% | `fundamental.json`(PE/PB/ROE/增速) | `"{name} {code} 估值 业绩 {YYYY}年{M}月"` | `"风险 下滑 亏损"` |
| 情绪面 | 15% | 无自动化 | `"{name} 涨跌停 换手率 板块 {YYYY}年{M}月"` | `"下跌 跌停 恐慌"` |
| 宏观面 | 10% | `macro_snapshot.json`(汇率/利率/PMI) | `"今日宏观 政策 利率 汇率 外盘 {YYYY}年{M}月"` | `"鹰派 衰退 收紧"` |

> `{YYYY}`和`{M}`取系统当前日期(见CLAUDE.md `# currentDate`)，严禁使用训练截止年份。

**自动化基线使用**：基本面读PE/PB百分位/增速/ROE；资金面读北向/融资数据；宏观面读汇率/PMI/CPI/利率。数据质量`good`/`partial`时可用。Agent评分始终覆盖自动化评分。

**维度摘要**(每个非技术维度1-2句，利多+利空双方向)：
- 格式：`利多：xxx；利空：xxx`(分号分隔)。只有单向时标注"未找到反向信号，可能存在确认偏差"。
- 资金面例：`利多：ETF近20日净申购+1.75亿元；利空：主力近2日净流出1.59亿元`

摘要通过`--*-summary`传入Step 3。**综合研判**(核心矛盾/关键事件/操作建议)通过`--analysis`传入。

**搜索工具**：使用当前环境可用的联网搜索/网页工具，优先官方公告、交易所、监管机构和数据源；脚本内置数据源作为结构化行情的降级路径。不要直接抓取限制自动访问或结果不稳定的网页（包括 `*.eastmoney.com`、`cn.investing.com`、`xueqiu.com`、`10jqka.com.cn`），需要东方财富数据时优先运行已有 fetcher。

### C. 数据质量

检查 `technical.json` 的 `summary.data_quality`：`insufficient`(<30条)→技术面权重17.5%，`limited`(30-59条)→25%。

### D. 等待汇合

管线完成+搜索收齐→Step 3。管线超时维度按0分，"管线超时"标注。

### E. 逆向校验

每非技术维度检查：
1. 覆盖≥2个必检项？
   - 资金面：主力净流入(P0)、北向/南向(P1)、IOPV折溢价(P0·ETF)
   - 基本面：PE/PB估值分位(P0)、盈利增速(P0)、NAV/跟踪误差(P0·ETF)
   - 情绪面：涨跌停/板块联动(P0)、新闻舆情(P0)
   - 宏观面：货币政策(P0)、外盘影响(P1)
2. 同时含利好+利空？
3. 单一事件≤封顶值(宏观1.5/其他1.0)？
4. 逆向审视：假设方向错误，最强反向论据？
5. 反向论据已在摘要中？

**未通过处理**：缺必检项→维度分×0.5；缺反向信号→一致性因子×0.6；超封顶→修正至封顶值；逆向调整→向0靠近0.5-1.0。

自检结果通过`--self-check`传入Step 3，JSON格式：
```json
{"capital_flow":{"counter_found":true,"adjusted":false,"covered_items":3},"fundamental":{"counter_found":true,"adjusted":false,"covered_items":2},"sentiment":{"counter_found":false,"adjusted":true,"original":1.0,"revised":0.5,"covered_items":2},"macro":{"counter_found":true,"adjusted":false,"covered_items":3}}
```

## Step 3: 综合评分

```bash
python3 .claude/skills/stock-trend/scripts/analysis/scores.py --code <code> \
  --capital-flow-score <N> --fundamental-score <N> --sentiment-score <N> --macro-score <N> \
  [--focus <维度>] [--asset-type etf|hk|st|stock] \
  [--self-check '...'] [--signals-info '...'] [--risks '...'] \
  [--capital-summary "..."] [--fundamental-summary "..."] [--sentiment-summary "..."] [--macro-summary "..."] \
  [--analysis '...']
```

脚本自动从 `.cache/stock-trend/{code}/` 读取 `technical.json` 及各维度数据。

**`--mode wyckoff`(P0-3 独立维科夫 100 分制)**:不进复合分,单跑维科夫出 0-100 分。分数 = 阶段分70%(`wyckoff_score` [-3,+3]→[0,100],quality limited×0.9/insufficient→50) + VSA20%(信号强度均值) + 置信度10%。输出含买点判定(吸筹/拉升阶段买点子阶段)与 verdict,写 `wyckoff_score.json`。
```bash
python3 .claude/skills/stock-trend/scripts/analysis/scores.py --mode wyckoff --code <code>
# 或直接给文件
python3 .claude/skills/stock-trend/scripts/analysis/scores.py --mode wyckoff --wyckoff-data <wyckoff.json> -o /tmp/wyckoff_score.json
```

**评分规则**：
- 技术面：从 `technical.json` 的 `summary.total_score` 自动提取
- 技术面子权重(脚本自动)：趋势(MA/MACD)×1.5、ADX×1.2、震荡(RSI/KDJ)×0.8、通道量能(布林/成交量/OBV)×1.0、K线形态×0.5。一致性因子=同向指标数/总指标数。
- 自动基线(AKShare数据,Agent未传score时生效)：PE/PB百分位<30→+1,>70→-1；利润增速>10%→+1；ROE>15%→+1；HS300涨>1%→+1,<-1%→-1；PMI≥50→+1；人民币升值→+1；北向增持→+1
- Agent显式`--*-score`**始终覆盖**自动基线
- 默认权重：技术35%/资金25%/基本15%/情绪15%/宏观10%
- `--focus`权重调整：technical→技术55%, capital_flow→资金50%, fundamental→基本45%, sentiment→情绪45%
- 数据质量调整：insufficient→技术17.5%, limited→25%
- **趋势判定**：≥+2.0看多，≤-2.0看空，其余震荡
- **置信度**：|score|≥2.5且一致性≥0.7→高；≥2.0且一致性≥0.5→中；其余→低
- 多周期共振(`--multi-timeframe`)：日周同向提一级，反向降一级
- 风险项从`key_signals`自动提取+去重
- ETF/HK/ST特殊标记自动生成
- `--analysis`结构化JSON：`{core_conflict, events:[{date,event,impact}|{name,detail,impact}], advice:[...]}`

输出 `.cache/stock-trend/{code}/scores.json`。

## Step 4: 风险管理 + 生成报告

**大盘/板块对比**(report.py 自动): 若 `.cache/stock-trend/market_regime.json` 存在(今日复盘生成),报告含「📊 大盘/板块对比」段 — 市场评分背景、个股 vs 沪深300 相对强弱、所属板块位置。缺失时提示先跑 `/daily-review`。

**风险管理**：基于`technical.json` summary→止损位、三级目标(保守/主目标/激进)、R:R比、仓位建议、最大回撤。监控信号按趋势方向生成。

**特殊标的**(脚本自动)：
- ST/*ST：基本面强制-1
- 港股：恒指联动、卖空占比、AH溢价
- ETF：IOPV折溢价、跟踪误差、基差/OI/期货量
- 可转债：转股溢价率、强赎风险

**生成报告**：
```bash
python3 .claude/skills/stock-trend/scripts/reporting/report.py --code <code> \
  --ts-code <ts_code> --stock-name '<名称>' \
  --output-md reports/stocks/<ts_code>/<YYYYMMDD-HHmm>.md \
  --output-html reports/stocks/<ts_code>/<YYYYMMDD-HHmm>.html
```

**精简模式**(`--compact`)：
```
{名称}({代码}) {趋势符号}{方向} | 评分{总分} | 技术{技术分} 资金{资金分} 基本{基本分} 情绪{情绪分} 宏观{宏观分}
风险: {风险1}; {风险2}
支撑/压力: {支撑位}/{压力位}
入场时机: {入场建议} — {确认信号}
```
精简模式仅文本输出，不保存HTML。

**保存路径**：MD→`reports/stocks/{ts_code}/{YYYYMMDD-HHmm}.md`，HTML→`reports/stocks/{ts_code}/{YYYYMMDD-HHmm}.html`。`ts_code`用Tushare格式(含后缀,如159740.SZ)。

**默认模式**生成 HTML 后返回文件路径。只有用户明确要求查看时才打开该文件；`compact` 跳过 HTML。

## 免责声明

所有输出必须附带：**本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。**

## 参考文件

- 报告模板(MD): [assets/report-template.md](assets/report-template.md)
- 报告模板(HTML): [assets/report-template.html](assets/report-template.html)
