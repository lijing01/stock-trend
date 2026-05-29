# Scripts 重构计划

## 现状

31 个 Python 文件平铺在 `scripts/`，跨文件通信方式混用：直接 import + subprocess 调用 + 延迟 import。4 处 `run_script()` 重复实现，`BaseFetcher` 定义了但零个子类。16k 行代码，top 4 文件占 65%：`etf_scanner.py`(1964), `analyze_technical.py`(1921), `compute_scores.py`(1272), `portfolio_manager.py`(1169)。

### 关键约束

- **SKILL.md `allowed-tools`**：每个脚本的 CLI 路径显式白名单，移动文件必须同步更新
- **golden_config.json**：`scripts[].name` 映射到输出文件名，改名影响 golden 测试
- **Test 文件**：用 `SCRIPTS_DIR / filename` 硬编码路径调用脚本
- **内部 subprocess**：`run_pipeline.py`、`etf_scanner.py`、`market_leader.py` 中硬编码脚本文件名字符串

---

## 阶段 1：消除样板代码（低风险）

**目标**：不改路径，不改 CLI 接口，仅内部重构。

### 1a. BaseFetcher 落地

所有 `fetch_*.py` 脚本有相同样板：argparse → 缓存检查 → fetch → 写缓存 → 输出 JSON。

改为 `BaseFetcher` 子类，每个文件只写 `fetch()` 方法：

```
fetch_kline.py          → class KlineFetcher(BaseFetcher)
fetch_kline_eastmoney.py → class EastMoneyKlineFetcher(BaseFetcher)
fetch_capital_flow.py   → class CapitalFlowFetcher(BaseFetcher)
fetch_fundamental.py    → class FundamentalFetcher(BaseFetcher)
fetch_etf_data.py       → class ETFDataFetcher(BaseFetcher)
fetch_futures_data.py   → class FuturesDataFetcher(BaseFetcher)
fetch_index_valuation.py → class IndexValuationFetcher(BaseFetcher)
fetch_macro_snapshot.py → class MacroSnapshotFetcher(BaseFetcher)
```

每个 fetch 减少 ~40 行样板。CLI 接口保持完全兼容。

### 1b. 合并 `run_script()`

4 处重复实现 → 统一到 `cache_utils.py`：

```python
def run_script_cmd(cmd, label="", timeout=30):
    """接受完整 cmd list，返回 {success, label, returncode, stdout, stderr, timeout}."""

def run_script_file(script_name, *args, timeout=30):
    """接受脚本名 + 参数，自动拼接 SCRIPT_DIR 路径."""
```

影响文件：`run_pipeline.py`, `etf_scanner.py`, `portfolio_manager.py`, `diagnose.py`

### 1c. 删除重复工具函数

- `diagnose.py:208,224` 自有 `load_cache/save_cache` → 删除，import `cache_utils`
- `fetch_etf_data.py:25` 自有 `_fetch_url` → 删除，import `eastmoney_utils.fetch_url`
- `analyze_market_theme.py:36-44` 条件 import 块 → 统一为直接 import

### 阶段 1 影响范围

| 项目 | 数量 |
|------|------|
| 改动文件 | ~16 |
| 测试改动 | 无（接口不变） |
| 风险 | 低 |

---

## 阶段 2：分包重构（中风险）

**目标**：按领域分包，import 路径自文档化。一步到位，不留过渡状态。

### 2a. 目标包结构

```
scripts/
├── __init__.py              # 导出常用公共 API
├── core/
│   ├── __init__.py
│   ├── cache_utils.py       # 缓存、safe_float、TTL、output_json、run_script
│   ├── eastmoney_utils.py   # EM API 工具、piecewise_linear、build_secid
│   ├── base_fetcher.py      # BaseFetcher 基类
│   └── resolve_code.py      # 代码解析
├── fetchers/
│   ├── __init__.py
│   ├── kline.py             # 原 fetch_kline.py
│   ├── kline_eastmoney.py   # 原 fetch_kline_eastmoney.py
│   ├── capital_flow.py      # 原 fetch_capital_flow.py
│   ├── etf_data.py          # 原 fetch_etf_data.py
│   ├── fundamental.py       # 原 fetch_fundamental.py
│   ├── futures_data.py      # 原 fetch_futures_data.py
│   ├── index_valuation.py   # 原 fetch_index_valuation.py
│   ├── macro_snapshot.py    # 原 fetch_macro_snapshot.py
│   ├── sector_data.py       # 原 fetch_sector_data.py
│   ├── sector_kline.py      # 原 fetch_sector_kline.py
│   ├── ddx.py               # 原 fetch_ddx.py
│   └── longhubang.py        # 原 fetch_longhubang.py
├── analysis/
│   ├── __init__.py
│   ├── technical.py         # 原 analyze_technical.py
│   ├── chip_distribution.py # 原 compute_chip_distribution.py
│   ├── scores.py            # 原 compute_scores.py
│   ├── market_theme.py      # 原 analyze_market_theme.py
│   └── quality_gate.py      # 原 quality_gate.py
├── scans/
│   ├── __init__.py
│   ├── etf_scanner.py
│   └── market_leader.py
├── pipeline/
│   ├── __init__.py
│   └── runner.py            # 原 run_pipeline.py
├── reporting/
│   ├── __init__.py
│   ├── report.py            # 原 generate_report.py
│   └── chart.py             # 原 generate_chart_html.py
├── portfolio/
│   ├── __init__.py
│   └── manager.py           # 原 portfolio_manager.py
├── backtesting/
│   ├── __init__.py
│   └── engine.py            # 原 backtest_engine.py
└── diagnose.py              # 保持顶层
```

### 2b. Import 策略

统一使用 `sys.path` + 稳定绝对 import：

```python
import sys
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))  # scripts/

from core.cache_utils import load_cache, safe_float
from fetchers.kline_eastmoney import fetch_eastmoney
```

CLI 独立运行和 import 调用两用兼容。

### 2c. 需同步更新的文件

| 影响源 | 改动量 | 说明 |
|--------|--------|------|
| SKILL.md | 18 行 | `Bash(python3 .../xxx.py *)` 路径更新 |
| golden_config.json | ~11 项 | `scripts[].name` 映射 |
| test_stock_trend.py | ~20 处 | subprocess 调用 + sys.path |
| test_etf_scanner.py | 3 处 | sys.path + import |
| test_longtou.py | ~10 处 | 同上 |
| test_golden.py | ~5 处 | 脚本路径 |
| test_golden_regressions.py | ~3 处 | sys.path |
| test_market_theme.py | 3 处 | 同上 |
| test_quality_gate.py | 2 处 | 同上 |
| 内部 subprocess 调用 | ~15 处 | 脚本名字符串 |
| 内部直接 import | ~30 处 | import 路径 |

### 2d. `__init__.py` 导出

```python
# scripts/__init__.py
from scripts.core.cache_utils import (
    load_cache, save_cache, safe_float, output_json, get_market_day_ttl
)
from scripts.core.eastmoney_utils import (
    piecewise_linear, piecewise_linear_clamped, build_secid
)
```

子包 `__init__.py` 选择性导出常用符号。

### 阶段 2 影响范围

| 项目 | 数量 |
|------|------|
| 搬动文件 | 31 |
| 新建目录 | 8 |
| 新建 __init__.py | 8 |
| 更新 import | ~50 处 |
| 更新 SKILL.md | 18 行 |
| 更新 golden_config.json | ~11 项 |
| 更新 test 文件 | 7 个 |
| 风险 | 中 |

---

## 阶段 3：Subprocess → 直接调用（高风险）

**目标**：`run_pipeline.py` 内部管线步骤从 subprocess 转为直接 import 调用。SKILL.md 触发的顶层 CLI 入口不变。

### 3a. 每个脚本拆 IO 和逻辑

```python
# fetchers/kline.py
def fetch_kline_to_file(ts_code, output_path, **kwargs):
    """可直接 import 调用。写 output_path，返 result dict。"""
    ...

def main():  # CLI 入口，保持兼容
    ...

if __name__ == "__main__":
    main()
```

### 3b. run_pipeline.py 改为直接调用

```python
# 现状
kline_result = run_script([sys.executable, str(SCRIPT_DIR / "fetchers/kline.py"), ts_code, ...])

# 改为
from scripts.fetchers.kline import fetch_kline_to_file
# 在 ThreadPoolExecutor 中调用，try/except 保持失败隔离
```

### 3c. 收益

- 消除每个步骤 ~100ms Python 解释器启动开销
- 错误返回具体异常对象，不再解析 stderr 字符串
- 调试时可打断点跟踪全链路
- 不再需要 `run_script()` 包装

### 3d. 风险

- 失去进程级隔离（某个步骤 C 扩展崩溃会带崩管线）
- 补偿：`try/except` + `ThreadPoolExecutor` 超时机制

### 阶段 3 影响范围

| 项目 | 数量 |
|------|------|
| 需拆 IO/逻辑的脚本 | ~10 |
| run_pipeline.py 重写 | 1 |
| 风险 | 高 |

---

## 执行顺序

| 顺序 | 阶段 | 预估时间 | 风险 |
|------|------|---------|------|
| 1 | 阶段 1a：BaseFetcher 落地 | 30min | 低 |
| 2 | 阶段 1b：合并 run_script | 15min | 低 |
| 3 | 阶段 1c：去重工具函数 | 10min | 低 |
| 4 | 阶段 2：分包重构 | 2h | 中 |
| 5 | 阶段 3：subprocess 消除 | 3h | 高 |

阶段 1 可合并为 1 个 PR。阶段 2 单独 PR。阶段 3 单独 PR，全量测试通过后再合。

每个阶段完成后必须执行：
```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```
