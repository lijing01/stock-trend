# 今日复盘展示今日推荐观察池

## 目标与已核实事实

- 将今日复盘 HTML 的“观察列表”改为今日推荐的“观察池”展示样式：区块标题仍为“观察列表”，显示数量及 `#`、`名称 / 代码`、`板块`、`小级别维科夫阶段`、`短线买点`、`短线置信度`、`原始 / 质量 / 优先`、`新闻净调整 / 影子分`、`数据覆盖`、`数据问题与原因` 十列。
- 指定的 [候选报告](/Users/jing.li7/personal/stock-trend/reports/lists/candidates-20260921-171908.html:97) 含观察池 26 行。[正式快照](/Users/jing.li7/personal/stock-trend/.cache/stock-trend/recommendation_history/2026-09-21.json) 的 `content.buckets.observation` 也是同序 26 行，但不含 `news_analysis`；因此它不能单独复刻指定 HTML 的新闻列。
- 当前复盘从 YAML 读取四只手工观察对象并渲染三列，见 [market_regime.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/analysis/market_regime.py:696)；今日推荐 JSON 已输出完整 `observation`，见 [daily_candidates.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/scans/daily_candidates.py:4277)。已有十列表格和观察池行渲染，见同文件 [表格函数](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/scans/daily_candidates.py:3803)。

## 实施步骤

1. 在 [daily_candidates.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/scans/daily_candidates.py:3803) 提取可复用的“观察池区块”渲染入口，复用现有行、表头、分级说明和诊断文案。为复盘提供所需 CSS，处理窄屏横向滚动；候选报告仍调用同一入口。确保动态文本转义，正式推荐分桶、打分和排序不变。
2. 候选扫描生成结构化结果时，同步把本次 `buckets.observation`（包含新闻影子字段）、依据日、生成时间和可用的候选报告路径冻结为同名结构化副产物；即使调用方传 `--no-html` 也保留观察池数据。副产物写入失败仅在复盘更新状态中呈现，不改变候选推荐结果。[run_today.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/bridge/run_today.py:233) 把该次候选产物的路径或摘要传给独立复盘更新任务，不按“最新文件”猜测来源。
3. [market_regime.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/analysis/market_regime.py:747) 保留现有 `OBSERVATION_LIST` 标记与原子替换机制；新内容由冻结观察池渲染，显示候选依据日与来源报告链接。独立 `/daily-review` 只查找与市场 `data_date` 一致且完整的候选副产物；缺失时显示“同日候选观察池尚未生成/不可用”，不把 YAML 四只或其他日期快照冒充本次观察池。`--no-refresh` 同样按缓存市场依据日匹配。
4. [today_background.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/bridge/today_background.py:160) 在候选扫描终态后读取任务绑定的冻结数据并更新原复盘 HTML 路径，继续保持“先给初版链接，后台更新同一文件”。候选扫描失败时报告保留待更新/失败提示；复盘任务失败只影响该区块与任务状态。验证复盘与候选数据日期相同，记录产物摘要与来源。
5. 对现有 [复盘报告](/Users/jing.li7/personal/stock-trend/reports/lists/daily-review-20260922-001728.html) 做一次历史回填：只从用户指定的 2026-09-21 候选 HTML 读取“观察池”表格与说明，核对 26 行、代码顺序和十列后替换复盘标记区块；保留原复盘链接及市场内容。历史报告的市场分数 68.6 与候选报告中的 69.0 属不同生成时点，展示来源时间，不混合或重算评分。此 HTML 提取仅用于历史回填，日常更新使用结构化副产物。
6. 更新 [SKILL.md](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/SKILL.md:230) 中的来源、独立复盘缺源行为和同次更新语义；注明 YAML 留作手工观察配置，不再作为该 HTML 区块的数据源。

## 验收与验证

- 指定 9 月 21 日复盘 HTML 的“观察列表”恰有 26 行，代码顺序与指定候选 HTML 完全一致；十列内容、新闻影子分和观察分级说明一致。复盘区块外内容不变，原路径可打开。
- 新的今日推荐运行中，初版复盘路径在候选扫描前输出；候选完成后原路径更新为同次 `observation`，候选报告路径和正式分桶结果保持不变。观察池为 0 时显示 0 与空态。
- 同日结构化源缺失、损坏、日期不匹配或后台失败时，复盘显示明确状态且不会使用其他日期的观察池；候选结果仍可交付。
- 定向覆盖源绑定、26 行顺序、新闻列、HTML 转义、独立 `/daily-review --no-refresh`、原子替换与失败隔离；再运行仓库必需门槛 `test_stock_trend.py` 和 `test_golden.py --diff`。不因格式变动更新黄金快照。

## 风险与处理

- 正式推荐快照会归一化日期且没有新闻影子字段，历史回填必须以指定候选 HTML 为准；今后的结构化副产物必须在报告渲染时冻结同一批对象。
- 宽十列表格需要复用候选页的滚动容器和必要 CSS；不得只复制 `<table>` 而丢失可读性或“观察｜不可执行”标识。
- 候选与复盘即使依据日相同也可能在不同时间获得不同市场分数；展示各自来源时间，不覆盖复盘市场评分。
