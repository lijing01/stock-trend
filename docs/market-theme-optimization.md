# 市场主线分析优化计划

## 概述

`analyze_market_theme.py` 负责板块持续性和市场主线判断。当前版本存在 3 类 P0 逻辑缺陷、若干代码质量问题。

---

## P0 — 逻辑缺陷

### 1. `--days` 参数未用于数据窗口截断

**问题**: `_up_days_ratio`、`_compute_volatility`、`_compute_acceleration` 消费 API 返回的全量 K 线（~28 条），而非 `--days N` 指定的窗口。

**影响面**:
- `up_days_ratio` 计算 28 天上涨占比，但报告显示 `{up_days_ratio*10:.0f}/{lookback_days}`（28 天数据 ÷ 10 天窗口），数值错误
- `volatility` 跨 3 个月数据计算，不能反映近 N 天真实波动
- `acceleration` 最近 3 天 vs 前 7 天，同样基于全量而非窗口

**修复方案** ✅ 2026-05-28: `compute_persistence` 增加 `lookback_days` 参数，函数入口先截断 `kline[-lookback_days:]`，所有子计算共用同一窗口。

### 2. `min_score=30` 制造不可见空白 (已修复)

**问题**: `main()` 过滤 `persistence >= 30`，但 `classify_themes` 将 `< 40` 定义为"退潮"。导致:

- 30-39 分的板块归入退潮
- 0-29 分的板块被静默丢弃
- 退潮板块列表不完整，用户低估退潮规模

**修复方案** ✅ 2026-05-28: `--min-score` 默认值从 30 改为 0，让 `classify_themes` 全权负责分类，退潮板块完整展示。

### 3. `hot_threshold = max(results) * 0.7` 不稳定 (已修复)

**问题**: 脉冲热点判别阈值依赖全局最高值。弱市普跌时所有板块热度偏低，阈值过低导致标记失效；个别板块极高时正常板块也被标记为一日期游。

**修复方案** ✅ 2026-05-28: 双阈值 `max(60, max_hot * 0.6)` — 绝对下限 60 防弱市误报，相对 60% 防单 outlier 拖拽。

---

## P1 — 代码质量

### 4. HTML 模板大量重复

`rank_table` 和 4 个分类 section（`strong/moderate/emerging/fading`）行模板高度相似，约 100 行重复代码。622 字节 CSS 嵌入 f-string，可维护性低。

**修复方案**: 提取行渲染函数或迁移至 Jinja2 模板文件。

### 5. `sys.path.insert(0, ...)` 脆弱

```python
sys.path.insert(0, str(SCRIPT_DIR))
```

目录结构变化或跨脚本组合调用时易出错。

**修复方案**: 改用 `python -m` 调用或包级相对导入。

### 6. 无测试覆盖

其他分析模块有测试，此文件为零。核心评分函数 `compute_persistence` 和分类函数 `classify_themes` 缺乏边界条件验证。

**修复方案**: 创建 `test_market_theme.py`，覆盖:
- 空 K 线、短 K 线（< 3 条）
- `--days` 窗口截断正确性
- 各分类阈值边界（39/40/49/50/69/70）

---

## P2 — 性能/体验

### 7. 无数据缓存

每次运行重复请求排名 API + BK K 线 API。日内多跑一次浪费一次。板块排名半小时内变化极小。

**修复方案**: 利用现有 `cache_utils.py` 缓存板块排名结果（TTL 30min）。

### 8. `max_workers` 硬编码 4

`--top 50` 时同为 4 线程。应随 `--top` 自适应。

**修复方案**: `min(top_n // 4 + 1, 8)` 或暴露为 CLI 参数。

---

## 修复优先级

| 优先级 | 项 | 状态 |
|--------|----|------|
| **P0** | `--days` 窗口截断 + up_days_ratio 显示修正 | ✅ 已修复 |
| **P0** | `min_score` 过滤逻辑重设计 | ✅ 已修复 |
| **P0** | `hot_threshold` 判别改进 | ✅ 已修复 |
| **P1** | HTML 模板去重（提取行渲染函数） | ~30min |
| **P1** | 添加测试 `test_market_theme.py` | ~1h |
| **P2** | 引入缓存 | ~30min |
| **P2** | `max_workers` 自适应 | ~10min |
