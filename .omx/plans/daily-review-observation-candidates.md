# 今日复盘 HTML 分阶段展示观察候选股

## 需求与边界

- 今日复盘 HTML 与今日推荐候选报告仍是两个独立产物，共用已有市场上下文。候选报告的生成、资格和排序不依赖复盘 HTML 是否更新成功。
- 先生成可访问的市场复盘 HTML，并立即输出其链接；候选扫描继续执行。候选阶段结束后，由后台任务读取 `.claude/skills/stock-trend/data/observation_list.yaml` 的 `observation_list`，更新**同一 HTML 路径**的观察候选区块。
- 区块标题用“观察列表”，按 YAML 原顺序列出 `code`、`date`（加入观察日期）、`entry_phase`（加入时阶段），明确“仅为观察候选，正式推荐见候选报告”。不从候选扫描结果替代 YAML 列表，也不推断名称、评分、价格或交易建议。

## 实施步骤

1. 在 [market_regime.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/analysis/market_regime.py:1018) 给 HTML 增加稳定的观察候选容器。首次写入显示“候选扫描中，观察列表待更新”；提供独立、只读的 YAML 加载和安全渲染函数，所有文本做 HTML 转义。空列表、缺文件、非法 YAML 和缺字段都有明确提示；格式异常代码保留原值并标记异常，不改写 YAML。
2. 在 [run_today.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/bridge/run_today.py:209) 保留“市场刷新成功 → 候选扫描”的前置关系，因为候选脚本读取本次 `market_regime.json`。市场刷新同时生成初版复盘 HTML；确认文件存在后，立刻向进度通道输出绝对路径/链接并刷新缓冲，不等候选扫描或研究后处理结束。最终结构化结果也保留这一路径。单独 `/daily-review` 生成 HTML 时直接展示当时的 YAML 列表，不需要扫描状态。
3. 候选扫描进入终态后，在 [run_today.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/bridge/run_today.py:216) 启动独立后台 HTML 更新任务。任务只读取本次初版报告和当时 YAML，校验报告标识/路径后替换观察候选容器，写临时文件再原子替换原 HTML；不修改候选 JSON、MD、正式推荐快照或市场上下文。扫描失败也记录终态并允许区块更新，不能因 HTML 更新失败压制候选结果。后台状态可查询，失败时初版报告继续可打开并显示“待更新/更新失败”。
4. 在 [test_market_regime.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/tests/test_market_regime.py:1) 和 [test_run_today.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/tests/test_run_today.py:1) 添加分阶段契约测试；更新 [stock-trend/SKILL.md](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/SKILL.md:207) 的真实 YAML 路径、链接输出顺序和后台更新语义。

## 验收标准

- 市场刷新成功后、候选扫描开始前，初版 `daily-review-*.html` 已存在，进度输出含可打开的绝对路径；候选扫描完成后最终结果仍包含自己的候选报告路径。
- 后台更新完成后，**原复盘链接不变**，HTML 中的股票行与任务读取时 YAML 的 `observation_list` 顺序和字段一致；观察区块之外的市场分数、板块和提示保持字节级或结构级一致。
- 模拟后台更新失败、缺 YAML、恶意 HTML 字符、五位代码及候选扫描失败：候选报告/状态按原逻辑交付，初版复盘仍可打开，错误在复盘区块或任务状态中可见。
- `/daily-review --no-refresh` 单独运行时直接生成包含观察列表的 HTML；JSON 市场上下文和评分不新增候选字段。

## 验证与风险

- 先跑针对性单测和隔离目录的分阶段集成测试，验证“链接先可用 → 候选结束 → 原路径内容更新”的时序与原子替换；再运行必需质量门：`/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py` 与同解释器运行 `test_golden.py --diff`。黄金快照只在确认变更符合预期后更新。
- 当前 CLI 只在所有步骤结束后打印最终 JSON；早期链接须走不破坏 JSON 的进度通道，并由调用方在收到路径时先交付给用户。若某调用方只读取进程结束后的输出，则需要同步修改该调用方的流式消费方式，否则无法保证“先给链接”的交互时序。
- 当前工作树中的 YAML 有未提交改动，只读取且绝不覆盖。后台更新只允许作用于本次运行创建、位于报告目录内的 HTML，避免并发运行互相覆盖。

停止条件：上述时序、内容及故障隔离验收通过；今日推荐的生成不因复盘 HTML 后台更新而等待或降级。
