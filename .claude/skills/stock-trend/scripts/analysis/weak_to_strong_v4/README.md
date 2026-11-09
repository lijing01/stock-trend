# 弱转强 V4

每日候选扫描使用本目录的 V4 实现，复用扫描阶段采集的日线和收盘确认数据。输出为 `weak_to_strong_v4`，T1、T2 分组按模型预期净收益及胜率分别排序；V4 只处理当日新确认事件。

扫描默认加载 `.cache/stock-trend/models/weak_to_strong_v4/YYYY-MM.json`（按扫描依据月选择）。环境变量 `STOCK_TREND_V4_BUNDLE` 和 `daily_candidates.py --weak-to-strong-v4-bundle 路径` 可覆盖默认路径，命令行参数优先。模型包含 T1/T2、版本、日期和内容哈希，使用本目录 `train.py` 从真实已闭合历史标签训练。模型缺失、无效、过期或特征不足时，报告显示状态与原因，不生成伪造的 V4 排名。模型仍为研究用途，效果需独立验证。

旧版弱转强的快照和跟踪不会作为 V4 的历史结果使用，当前 V4 报告不提供跨日跟踪。

## 月度模型训练

训练入口使用冻结的成熟 T1/T2 研究记录和权威交易日历，固定按单边 0.1% 成本生成净收益标签。月初模型只接受训练隔离日前的信号、训练截止日前已闭合的结果，并要求 T1/T2 各自至少覆盖 120 个信号日。模型及相邻 manifest 均为不可变产物；相同内容可幂等重跑，不同内容拒绝覆盖。

```bash
PYTHONPATH=.claude/skills/stock-trend/scripts \
bash tools/python.sh -m analysis.weak_to_strong_v4.train \
  --training /absolute/path/to/frozen-shards-a \
  --training /absolute/path/to/frozen-shards-b \
  --calendar .cache/stock-trend/trading_calendar.json \
  --validation-start YYYY-MM-DD \
  --output .cache/stock-trend/models/weak_to_strong_v4/YYYY-MM.json
```

训练数据路径仅在显式训练时使用。日常候选扫描只读取本仓按月模型，不依赖训练数据所在仓库。模型记录训练文件哈希、日历哈希、截止日、隔离日、成本和已知局限；历史研究样本不构成独立前向证据，V4 仍只用于研究观察。
