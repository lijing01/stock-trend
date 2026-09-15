# 候选报告评分单元格层级调整计划

## 需求摘要

调整“每日候选股”HTML 与 Markdown 报告中两个评分列的视觉层级：把**优先**分和**影子**分放在各自单元格第一行并加粗；把原始分、质量分、新闻净调整与新闻状态放在第二行，使用普通字重。评分计算、排序、资格、JSON 输出和字段含义保持不变。

本计划将用户所说的“优秀”按现有报告字段理解为“优先”。若实际希望新增一个名为“优秀”的指标，应先补充其数据来源与计算口径。

## 已确认实现位置

- HTML 单行候选渲染：`.claude/skills/stock-trend/scripts/scans/daily_candidates.py:3596`。
- 报表内联样式：同文件 `:3910` 起。
- 现有 HTML/Markdown 契约测试：`.claude/skills/stock-trend/tests/test_daily_candidates.py:1744` 起。
- Markdown 表格渲染：同文件 `:2616` 起；可使用表格单元格内的 `<br>` 与 `**…**` 表达同一层级。

## 实施步骤

1. 在 `_append_candidate_table()` 的两个 Markdown 评分单元格中重排内容：
   - 使用 `**优先 {score}**<br>原始 {score} · 质量 {score}`。
   - 使用 `**影子 {score}**<br>新闻 {delta} · {status}`。
   - 不改变数值来源或列标题，确保 Markdown 使用者也获得一致的阅读顺序。
2. 在 `_candidate_html_rows()` 的两个评分 `<td>` 中重排内容：
   - 第一列先输出 `<strong>优先 {score}</strong>`，随后换行；第二行输出普通文本 `原始 {score} · 质量 {score}`。
   - 第二列先输出 `<strong>影子 {score}</strong>`，随后换行；第二行输出普通文本 `新闻 {delta} · {status}`。
   - 继续通过已有 `candidate_rank_score()`、`news_analysis.shadow_priority_score` 与 `escape()` 获取值，避免任何计算或安全处理回归。
3. 只在必要时为上述 HTML 评分行添加语义 CSS 类（如 `score-primary`、`score-secondary`），确保“加粗/普通字体”由类稳定表达；不修改表格列宽、色彩、分桶或任何业务规则。
4. 更新定向测试以断言 HTML 和 Markdown 的新顺序、换行与字重：`<strong>优先 …</strong>` / `**优先 …**`、`<strong>影子 …</strong>` / `**影子 …**` 均出现在第一行，其他字段位于随后的普通文本行；保留 JSON 断言，证明展示改动没有触及数据契约。
5. 运行定向测试后执行仓库规定的两项质量门：

   ```bash
   python3 .claude/skills/stock-trend/tests/test_stock_trend.py
   python3 .claude/skills/stock-trend/tests/test_golden.py --diff
   ```

   最后执行 `git diff --check`，并检查未改写 golden 快照。

## 验收标准

- HTML 和 Markdown 每行的“原始 / 质量 / 优先”列视觉上为：第一行粗体“优先”，第二行普通“原始 · 质量”。
- HTML 和 Markdown 每行的“新闻净调整 / 影子分”列视觉上为：第一行粗体“影子”，第二行普通“新闻 · 状态”。
- 所有数值、状态文字、排序及推荐分桶与改动前一致。
- JSON 输出格式保持兼容。
- 两项强制质量门及 diff 检查通过；golden 仅可读取比对，不能为掩盖失败而重生成。

## 风险与缓解

- **“优秀”并非“优先”的笔误**：实施前以本计划中的解释为准；若不符，暂停代码修改并确认新字段定义。
- **仅测试字符串存在而未验证层级**：测试将同时断言首行粗体标签和第二行内容，锁定布局契约。
- **Markdown 表格渲染器忽略换行/粗体**：使用仓库已采用的 HTML 行内标签兼容路径，并以生成文本断言锁定标记；HTML 报告仍是视觉呈现的权威验证面。
