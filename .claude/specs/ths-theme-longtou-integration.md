# ths-theme + longtou 整合设计

> 版本: v1  
> 日期: 2026-05-31  
> 状态: 已批准设计稿

---

## 1. 动机

ths-theme（同花顺板块热力）和 longtou（龙头股扫描）是项目中两个独立的能力：

- **ths-theme** → top-down 板块热度评分（行业heat + 涨停概念 + 龙虎榜机构资金）
- **longtou** → bottom-up 个股龙头扫描，产出 per-stock 评分和方向信号

两者当前独立运行、互不感知。用户持中线仓、上班族、不可盯盘，需要「先定方向、再选个股」的决策链条。

**核心思路**：ths-theme 作筛选器 → longtou 作放大器。热板块里找龙头。

---

## 2. 架构与数据流

### 2.1 现状

```
ths-theme (全市场板块) → 热力报告
longtou (全市场扫描)   → 龙头报告
       ↑ 两条独立线，数据无交集
```

### 2.2 改造后

```
ths-theme 跑全市场 → heat_score + zt_score 筛选 → qualified_sectors.json
                                                          ↓
                                                  market_leader 读此文件，只扫热板块
                                                          ↓
                                                  整合报告（板块热力 + 龙头清单 + 信号标签）
```

### 2.3 解耦原则

用中间文件解耦，不硬调：

| 文件 | 路径 | 格式 |
|------|------|------|
| 热板块输出 | `.cache/stock-trend/qualified_sectors.json` | JSON |
| 板块映射表 | `.claude/skills/stock-trend/config/sector_mapping.yaml` | YAML |

`qualified_sectors.json` 结构：

```json
{
  "date": "2026-05-31",
  "threshold": {"heat_min": 50, "zt_min": 50},
  "sectors": [
    {"name": "半导体", "heat_score": 78, "zt_score": 82, "lhb_score": 45, "lhb_direction": "净买"},
    {"name": "人形机器人", "heat_score": 65, "zt_score": 71, "lhb_score": 60, "lhb_direction": "净买"}
  ]
}
```

### 2.4 分类体系映射

ths-theme 用同花顺分类，longtou 用东方财富分类。两者不完全一致。

**方案**：分离显示，不合并分类体系。

- ths-theme 输出方向指引（同花顺概念/行业名称）
- longtou 读取方向指引，通过关键词映射表找到东方财富对应板块，在该板块内扫龙头
- 映射不到时，ths-theme 方向保留，longtou 标注"该板块未在东方财富找到"，仅做参考

映射表 `config/sector_mapping.yaml` 示例：

```yaml
# ths_name -> [em_sector_names]
半导体: ["半导体及元件", "半导体"]
人形机器人: ["机器人", "自动化设备"]
人工智能: ["人工智能", "计算机应用"]
```

映射表初始手动维护约 50 条核心板块，后续可增量。

---

## 3. 评分融合

### 3.1 龙头评分公式改造

**原公式**：

```
stock_composite = change_pct×50% + amount×30% + ranking×20%
```

**新公式**：

```
stock_base_score = composite（原）× 70%
sector_boost     = sector.heat_score/33.3 × 15% + sector.zt_score/33.3 × 15%
final_score      = stock_base_score + sector_boost
```

`sector_boost` 范围 0~+1.8（ths-theme 0-100 除以 33.3 压到 -3~+3 区间）。

逻辑：板块热力占 30% 权重（heat 和 zt 各 15%），个股自身质量占 70%，板块不喧宾夺主。

### 3.2 综合信号标签

| heat≥50 & zt≥50 | final_score | 标签 | 建议 |
|:---:|:---:|------|------|
| ✅ | ≥ 1.0 | **双强·龙头确认** 🟢 | 首选，可建仓 |
| ✅ | 0 ~ 1.0 | **双强·关注中** 🔵 | 板块有力，个股待确认 |
| ✅ | < 0 | **双强·无龙头** ⚪ | 板块热但群龙无首，观望 |
| ❌ | ≥ 1.0 | **龙头·板块待确认** 🟡 | 个股强但板块不一致，严设止损 |
| ❌ | < 0 | **弱势区** ⚫ | 不参与 |

### 3.3 LHB 叠加因子（信息层）

lhb_score ≥ 60 的板块在报告中标记为「机构净买入板块」，不硬编码到评分中。用户参考。

---

## 4. 报告格式

### 4.1 结构

```
# 市场热力 · 龙头整合报告 — YYYY-MM-DD

## 一、市场总览
- 热力板块数（双强共振）
- 最强板块 top 3
- 市场情绪定性（积极/中性/谨慎）

## 二、热力板块 · 龙头扫描
（每个热板块一张表）

### 板块：名称 （信号标签 🔵/🟢/🟡/⚪）
| 行业热度 | 涨停概念 | 机构资金 |
|---------|---------|---------|
| 78/100 🔥 | 82/100 🔥 | 净买4.2亿 ✅ |

| 排名 | 龙头 | 评分 | 方向 | 止损位 | 标签 |
|------|------|------|------|--------|------|
| 1 | XX | +2.1 | 看多 | 320.5 | 🟢 |
| 2 | XX | +1.5 | 看多 | 145.0 | 🟢 |

### 板块：名称 （信号标签）
...repeat

## 三、龙虎榜线索
- lhb_score ≥ 60 的板块 and 个股

## 四、风险提示
```

### 4.2 输出格式

- Markdown：输出到 `reports/lists/integrated_YYYY-MM-DD.md`
- HTML：输出到 `reports/lists/integrated_YYYY-MM-DD.html`，用颜色标签

### 4.3 颜色标签定义

| 标签 | CSS class | 颜色 |
|------|-----------|------|
| 🟢 双强·龙头确认 | `signal-strong` | 绿 |
| 🔵 双强·关注中 | `signal-active` | 蓝 |
| 🟡 龙头·板块待确认 | `signal-caution` | 黄 |
| ⚪ 双强·无龙头 | `signal-watch` | 灰 |
| ⚫ 弱势区 | `signal-avoid` | 不显示 |

---

## 5. 改动范围

### 5.1 新增文件

| 路径 | 用途 |
|------|------|
| `.claude/skills/stock-trend/config/sector_mapping.yaml` | 同花顺↔东方财富板块名称映射 |
| `.claude/skills/stock-trend/scripts/bridge/sector_feeder.py` | qualified_sectors 读写 + 映射查询 |
| `.claude/skills/stock-trend/scripts/bridge/integrated_report.py` | 拼接两份输出为整合报告 |

### 5.2 修改文件

| 路径 | 改动内容 |
|------|----------|
| `ths_theme.py` | 新增 `--export-sectors` 参数；新增导出逻辑 |
| `market_leader.py` | 新增 `--sectors-from` 参数；评分函数增加 sector_boost |
| `SKILL.md` | `/stock-trend` 触发改为 3-step pipeline |

### 5.3 不改文件

- `sector_mapper.py` — 已有正常工作
- `sector_data.py` — longtou 原东方财富接口不动
- `zt_replay.py` — 涨停数据抓取不动
- `longhubang_agg.py` — LHB 聚合不动
- `weekly_report.py` — 后续再考虑整合
- `market_leader.py` 中的龙虎爬虫 — 不动，保留原独立风险标记

### 5.4 SKILL.md pipeline

```
/stock-trend:
  step 1: ths_theme.py [原有参数] --export-sectors
  step 2: 若 qualified_sectors.json 非空 → 
           market_leader.py --sectors-from qualified_sectors.json [原有参数]
  step 3: bridge/integrated_report.py 拼接 → 整合报告

/longtou: 保留全市场扫描模式（不变）
```

---

## 6. 边界情况处理

| 场景 | 行为 |
|------|------|
| 无热板块（双强为0） | 不跑 longtou，输出仅 ths-theme 热力报告 + 提示无强信号 |
| 映射找不到东方财富板块 | 在报告中标注"东方财富无对应板块"，保留方向参考 |
| ths-theme 失败 | 降级为原本 longtou 全市场扫描 |
| longtou 失败 | 降级为原本 ths-theme 热力报告，不阻塞 |

---

## 7. 测试计划

### 7.1 单元测试

- `sector_mapping.yaml` 读取与查询
- `qualified_sectors.json` 读写
- 龙头评分公式改造（原 vs 新对比）
- 信号标签判定逻辑

### 7.2 集成测试

- `--export-sectors` 输出格式验证
- `--sectors-from` 输入解析与降级
- 整合报告 MD/HTML 生成

### 7.3 Golden snapshot

- 整合报告 golden 快照，含各信号标签
- `test_golden.py --diff` 确保后续改动不破坏

---

## 8. 后续可扩展（非 v1）

- weekly_report 吸收融合评分
- 双分类体系自动映射（AI匹配）
- 龙虎榜追踪器（lhb_tracker）验证"双强板块 predict 龙头收益"
- signal_strength 时序衰减
