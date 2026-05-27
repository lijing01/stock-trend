# Code Review: stock-trend 工程

Date: 2026-05-27
Scope: `.claude/skills/stock-trend/scripts/` + `tests/`
Commit: c25e4ec

---

## Bug

### 1. `compute_scores.py` validate_input 校验阈值错误

File: `.claude/skills/stock-trend/scripts/compute_scores.py:617-618`

`validate_input()` 检查维度得分是否在 [-100, 100] 区间内，但实际维度分范围是 [-3, +3]（所有自动化评分函数均 clamp 到此范围）。此校验会产生误报。

```python
# 当前 (line 618):
elif score < -100 or score > 100:
# 应改为:
elif score < -3 or score > 3:
```

---

### 2. 同花顺请求使用 HTTP 非 HTTPS

File:
- `fetch_ddx.py:21` — `http://data.10jqka.com.cn/financial/ddx/opendata/`
- `fetch_longhubang.py:13` — `http://data.10jqka.com.cn/financial/longhubang/`

同花顺全站 HTTPS，HTTP 请求可能被重定向、拦截或降级。风险随浏览器策略收紧增长。

Fix: `http://` → `https://`

---

### 3. `risk_keywords` 列表重复项

File: `compute_scores.py:458`

```python
risk_keywords = ["背离", "死叉", "极度收口", "超买", "压力", "空头", "下跌", "减仓", "止损",
                 "净流出", "净流出"]
```

"净流出" 出现两次。不影响去重逻辑（`extracted_topics` set 兜底）但说明数据未清理。

---

### 4. `validate_event_cap` 可能收到 None 值

File: `compute_scores.py:380`

若 `scores` dict 中某维度为 None，`abs(score)` 抛 `TypeError`。嵌套调用链 `validate_dimension_scores` → `validate_event_cap` 未做防御。

---

### 5. `market_leader.py` direction 字段兼容性缺口

File: `quality_gate.py:52`

`check_signal_consistency` 用 `"偏多" in direction` 判断方向，但 `compute_scores.py:dimension_direction()` 可能返回纯 "看多"（score ≥ 2.0 时）。此时 `check_signal_consistency` 视作非多头方向，跳过冲突检查。

---

## Code Quality

### 6. `fetch_ddx.py` + `fetch_longhubang.py` 大量重复

| 重复内容 | 所在文件 |
|---------|---------|
| `_fetch_page()` 重试+退避+jitter | ddx:33-48, lhb:25-39 |
| `THS_HEADERS` dict | ddx:23-28, lhb:15-20 |
| `THS_DDX_URL` / `THS_LHB_URL` | ddx:21, lhb:13 |
| `_clean()` HTML 标签清洗 | ddx:66-67, lhb:70-71 |
| 正则 HTML table 解析 | ddx:51-103, lhb:59-112 |
| `FETCH_TIMEOUT` + `retries` 参数 | ddx:29-30, lhb:22-23 |

建议抽取 `ths_utils.py`。

---

### 7. 同花顺 HTML 解析脆性

两份 fetcher 均用正则解析 HTML table。同花顺前端改版即静默断裂（返回 None → 空数据 → 降级路径）。无 schema 校验或 HTML 结构变化检测。

---

### 8. `_parse_amount` 两个实现语义不同

- `fetch_longhubang.py:42-56`: 处理中文单位（万/亿），返回 yuan
- `fetch_sector_data.py:306-310`: 纯 float 转换，不处理单位

同名函数不同行为，import 时易混淆。

---

### 9. 函数内 import 模式

`compute_scores.py:177-178`, `189-190`:
```python
from cache_utils import CACHE_DIR
```
函数内 import 每次调用执行。应提至模块顶部。

`market_leader.py:295-297`:
```python
try:
    from quality_gate import check_signal_consistency
except ImportError:
    check_signal_consistency = None
```
循环内 import，每次候选迭代触发。应提至函数外。

---

### 10. 管线状态隐式依赖文件系统

`run_pipeline.py` 无显式状态机。步骤间依赖通过 JSON 文件存在性隐式传递。某步骤输出被删除或损坏，后续步骤静默跳过。

---

### 11. `build_special_section` 港股占位符裸露

`compute_scores.py:537-540`:
```python
content: "需补充恒指联动、卖空占比、南向资金、AH溢价数据"
```
占位符直接出现在输出报告中。建议降级为 None 或跳过生成。

---

### 12. `cache_utils.py` 路径推导依赖固定目录深度

```python
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_SCRIPT_DIR))))
```
4 级 `dirname` 硬编码。若脚本被 mv 或符号链接，路径断裂。

---

### 13. `market_leader.py` stdout 混合输出两种格式

L757: 报告文本 + JSON 通过哨兵行 `<!--JSON_OUTPUT-->` 分隔。下游子进程调用方需解析此格式。文档化或提供 `--json-only` 标志可降低耦合。

---

### 14. `eastmoney_utils.py` rotate 函数内 `import time`

`eastmoney_utils.py:125`:
```python
import time  # 函数内
```
代码短，但全文件仅此处用时，可提至模块顶。

---

### 15. `IOPV_HISTORY_CACHE_FILENAME` 全局单文件

`compute_scores.py:172`: 所有 code 的 IOPV 历史写入 `CACHE_DIR/iopv_history.json`。多标的并发写入（`market_leader.py` Phase3 多 worker）可能冲突。建议每 code 独立文件。

---

## Test Issues

### 16. 4 个测试函数定义但永不执行

`test_stock_trend.py:838-889`:
- `test_eastmoney_utils()`
- `test_base_fetcher_subclass()`
- `test_cache_dir_is_project_relative()`
- `test_clean_cache()`

定义在模块级且未被 `main()` 调用。写入了文件但从不运行。

---

### 17. 管线测试依赖网络无 mock

`test_stock_trend.py` `TP-PL-01` / `TP-PL-02` 需要真实 API 调用。CI 或无网络环境必失败。

`test_longtou.py` `test_get_sector_*` 系列同样问题。

---

### 18. `test_golden.py` ASSET_EXCLUSIONS 与 pipeline 逻辑重复

`test_golden.py:342-347`: 硬编码 dict 定义每种 asset 排除哪些文件。与 `run_pipeline.py` 管线逻辑重复。新增资产类型需同步修改两处。

---

### 19. Golden snapshot 阈值配置不可见

`tests/golden_config.json` 定义 numeric 比较阈值。diff 时需 `--regenerate` 才能理解阈值逻辑，降低了 golden 的可读性。

---

## 修改优先级建议

| 优先级 | 编号 | 原因 |
|--------|------|------|
| P0 | #1 | 校验误报导致无效告警 |
| P0 | #2 | HTTP 可能被浏览器拦截 |
| P0 | #4 | None 取值可崩运行时 |
| P0 | #16 | 死测试制造虚假安全感 |
| P1 | #5 | 方向判断遗漏场景 |
| P1 | #6 | 两份重复代码维护成本 |
| P1 | #7 | 第三方改版即可无声断裂 |
| P2 | 其余 | 可读性/设计，不影响正确性 |
