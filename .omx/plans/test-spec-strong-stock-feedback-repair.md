# 修复验收矩阵

1. 33只冻结名单主源全失败：输出33只、可用0、缺失33，均有终态原因；所有收益汇总null。池成功或失败均不删除昨日样本。
2. 东财主节点失败、轮换成功；全部东财失败、原生备用成功；源返回旧日、未来日、错误身份、缺价/量、缺原生pct：分别按证据恢复/拒绝，记录所有attempt与最终来源。
3. 腾讯qfq相邻收盘计算pct的现有函数不能直接进入原生反馈；原生适配器先有资料和脱敏响应fixture。复权/除权日用例不把派生收益冒充原生pct。
4. 盘中响应在15:10后才结束仍为盘中；13:24缓存不得因15:48读出成为收盘。日期当日但源事件时刻早/未知的报价按明示资格拒绝收盘统计，除非有经核验的完整收盘日线证据。
5. 同一上海日、权威日历确认交易、指数证据旧一日且个股证据当日：市场评分仍旧日/partial，反馈独立当日，两日期显式；上一交易日名单准确，推荐资格不放宽。
6. 假期、日历失效/覆盖不足、显式历史截止：不以当前日补历史。原有日历故障时允许冻结合格当日原始池的行为继续；不借此选历史相邻名单；calendar_invalid时断言未抓昨日反馈报价，但仍抓取并冻结合格当日池。
7. 新绑定日期/路径/摘要/截止任一篡改拒绝；旧绑定/旧artifact可读，缺诊断显示未保存；--no-refresh无网络且只能读原精确绑定。失败后同日重试产生新artifact，不改原报告或原artifact；首次cohort仍不可覆盖。
8. 总预算用模拟clock验证池耗时/内部HTTP回退/节点轮换/备用源共同扣减；≤4并发；主源慢时保留备用预算，未启动/超时各可识别；deadline后后台线程完成不能改变返回结果或落盘。
9. 上涨/下跌/平盘/明确停牌/缺失全部保留；BJ不被错误解释成SZ，未经核验备用源不尝试支持；数据部分恢复时汇总分母准确，跨日比较沿用原覆盖规则。
10. HTML/MD/JSON显示同一冻结时间、日期、覆盖、来源和最终缺口；0/33文案为全部缺失；恢复成功的provider告警不冒充最终缺失。
11. v1/v2 artifact/绑定/close读取版本分派；未知版本拒绝；v2收盘存储不覆盖同日v1；v1与v2完整反馈仍不可比较。所有schema/rule字段组合错配拒绝；新增字段不改变v1历史摘要。
12. resolve_feedback_session分别覆盖live_refresh/no_refresh/historical、report_as_of与runtime_start跨自然日、15:09→15:10跨阈值、假期/日历缺口。外层超时返回后，即使内层线程继续执行，也不得开始新节点/新源请求，返回对象及artifact保持不变；响应上限30秒＋0.2秒调度容差。

优先扩展既有test_market_feedback_fetcher.py、test_market_feedback.py、test_market_feedback_reporting.py、test_market_regime.py，并核验主测试入口确实执行新增用例。

实施后的命令（本轮规划不运行）：

```bash
bash tools/python.sh .claude/skills/stock-trend/tests/test_market_feedback_fetcher.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_market_feedback.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_market_feedback_reporting.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_market_regime.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_stock_trend.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_golden.py --diff
git diff --check
```

另对修改文件做解释器语法编译/仓库已有静态检查；不新增测试依赖。联网门禁及当日取数遵守local-runtime的沙盒外与进程级NO_PROXY契约。外部失败记录为验证缺口，不声称通过或自动改Golden。
