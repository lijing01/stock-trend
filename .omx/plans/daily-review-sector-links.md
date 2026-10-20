# 每日复盘板块东方财富链接计划

## 需求与范围

让复盘报告中板块名称可点击进入对应东方财富板块行情页。覆盖当日最强前10与最弱前3，HTML、Markdown同步；补充今天2026-10-20 13:37生成的已有报告，并让后续生成自动带链接。本轮交付计划，执行阶段再修改代码及报告。

页面格式已联网核对：`https://quote.eastmoney.com/bk/90.BK1295.html` 对应“其他数字媒体”。来源：https://quote.eastmoney.com/bk/90.BK1295.html 。

## 实施步骤

1. 在现有复盘渲染路径增加共用链接生成函数。
   - 修改 `.claude/skills/stock-trend/scripts/analysis/market_regime.py`，Markdown列表位于1553行附近，HTML列表位于2245行附近。
   - 使用板块记录中已保存的东方财富代码生成固定域名HTTPS链接；校验来源为eastmoney且代码格式有效。按代码定位，Ⅱ/Ⅲ等同名近似板块保持各自代码。
   - 来源缺失、非东方财富或代码非法时显示普通名称，不按名称猜测链接。HTML转义名称，Markdown转义名称中的特殊字符。
   - HTML名称链接在新标签页打开，带`rel="noopener noreferrer"`，提供清晰的悬停与键盘焦点效果；Markdown使用标准链接。
   - 板块区增加一句说明：外链显示东方财富当前行情，报告涨幅取自本次冻结快照。

2. 为今天已有的HTML及同名Markdown补充链接。
   - 目标为`reports/lists/daily-review-20261020-133744.html`及同名`.md`。
   - 使用与该报告生成时间、依据日、板块名称和涨幅匹配的冻结上下文中代码；不匹配时停止该报告补充，不借用其他运行的数据。
   - 复用`.claude/skills/stock-trend/scripts/core/report_file.py`的`report_lock`和`atomic_write_text`，沿用现有补充器HTML后Markdown的锁顺序；参考`bridge/review_detail_links.py:583`。
   - 首次写入保存`.pre-sector-links`备份；仅替换目标板块列表及链接说明。重复执行不得嵌套链接。任一文件失败明确报告各文件状态，重跑可恢复。

3. 验证链接、降级和报告内容。
   - 在`tests/test_market_regime.py`补充必要的渲染用例：正确BK链接、不同代码的Ⅱ/Ⅲ板块、无代码/非东财来源、名称转义。
   - 对报告补充检查HTML与Markdown均为13个匹配的板块链接，重复执行不增加链接；核对原顺序、名称、涨幅、评分、日期和其他区块保持原有值。
   - 用文本/HTML解析检查链接及新标签属性；联网核对所列10个强势板块页面标题与对应名称。无需启动浏览器GUI。

## 验收标准

- 用户列出的10个板块均可进入各自东方财富行情页，最弱前3采用同样交互。
- 后续新报告HTML和Markdown自动生成相同目标链接。
- 今天两份报告同步补充，报告中的+12.60%等原有数值与顺序保持原有值。
- 无有效东方财富代码的记录保留可读名称，不产生错误或猜测链接。
- 本地报告明确外站当前行情与本报告快照可能不同。

## 风险与处理

- 跨源分类代码不能直接拼接东财URL：先核验来源和BK代码。
- 今日快照与全局缓存可能不再对应：补充前核验运行身份及板块字段，缺失时保留原报告并报告缺口。
- 后台任务同时更新报告：使用已有报告锁和原子写入；两文件部分失败按文件报告并支持幂等重跑。

## 执行阶段验证命令

统一通过`bash tools/python.sh`选择解释器：

```bash
bash tools/python.sh .claude/skills/stock-trend/tests/test_market_regime.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_stock_trend.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_golden.py --diff
git diff --check
```

按仓库要求在修改Python脚本前说明影响范围；不为消除失败自动更新golden快照。

## 执行记录（2026-10-20）

- 已实现共用渲染、双文件补充CLI及使用说明，并补充今日13:37报告的HTML和Markdown。
- 13个东方财富板块页面均已联网核对标题、名称与代码；两份报告均有13个匹配链接。
- 与`.pre-sector-links`备份逐字比较，板块区之外内容完整保留；再次执行返回`unchanged`，报告及备份字节均未变化。
- 复盘专项：162通过；报告补充：4通过；阅读契约：4通过；Python编译与`git diff --check`通过。
- 最终主门禁：897通过、0失败、1跳过；跳过项为TF-CF-01股票资金流向联网超时。前一轮主门禁898通过、0失败。
- 最终Golden：21通过、0失败、2警告；警告为600519基本面、00700资金流来源不可用而延后语义比较。
