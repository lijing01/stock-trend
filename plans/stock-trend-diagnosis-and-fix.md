# Stock-Trend Skill 诊断与修复计划

## Context

stock-trend skill 在实际使用中存在数据获取降级链断裂、技术分析小数据误导、工作流步骤缺失等问题。需要设计一套诊断机制自动检测问题，修复已知bug，并用测试用例验证可靠性。

---

## 一、诊断 Agent 设计

创建 `.claude/skills/stock-trend/scripts/diagnose.py`，当 `/stock-trend` 触发时自动运行，检测以下项目并输出结构化报告：

### 诊断检查项

| 检查项 | 检测方法 | 失败标准 |
|--------|----------|----------|
| D1: Tushare可用性 | 尝试 `fetch_kline.py` 获取600519.SH | `data_source == "error"` |
| D2: 东方财富可用性 | 尝试 `fetch_kline_eastmoney.py` 获取600519.SH | `data_source == "error"` |
| D3: BaoStock可用性 | 诊断脚本内直接import测试 | import失败或查询返回0条 |
| D4: 港股数据可获取性 | 尝试获取00700.HK | 三级均失败 |
| D5: 周线数据可获取性 | 尝试东方财富周线 | `data_source == "error"` |
| D6: 数据量充足性 | 检查record_count < 60 | 不足60条发出警告 |
| D7: Python依赖完整性 | 检查tushare/baostock/numpy/pandas | import失败 |
| D8: Tushare Token配置 | 检查env/config文件 | 无Token |

### 诊断输出格式

```json
{
  "timestamp": "20260514-153000",
  "checks": {
    "tushare": {"status": "ok|error|unavailable", "detail": "..."},
    "eastmoney": {"status": "ok|error", "detail": "..."},
    "baostock": {"status": "ok|error|unavailable", "detail": "..."},
    "hk_support": {"status": "supported|unsupported", "detail": "..."},
    "weekly_support": {"status": "supported|unsupported", "detail": "..."}
  },
  "data_sources_priority": ["eastmoney", "baostock"],
  "warnings": ["Tushare Token无权限或未配置，已降级到东方财富"],
  "recommendations": ["港股需配置有效Tushare Token（需daily接口权限）"]
}
```

### 触发机制

在 SKILL.md 的 Step 3 之前插入诊断步骤：
- 首次使用 `/stock-trend` 时自动运行完整诊断
- 后续使用时仅检查数据源可用性（轻量模式，<5秒）
- 诊断结果缓存到 `/tmp/stock-trend-diag.json`，有效期1小时

---

## 二、Bug 修复

### Fix 1: 港股数据获取（D1）✅ 已修复

**问题**: 港股三级降级全失败，用户只能看到error。

**方案**: 在 `fetch_kline_eastmoney.py` 中增加港股数据源——使用腾讯财经API获取港股数据：

```python
def fetch_hk_stock(ts_code, freq, lmt=250):
    """使用腾讯财经API获取港股K线数据"""
    code = ts_code.split(".")[0]  # 00700
    # API: https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=00700&scale=240&ma=no&datalen=250
```

**文件**: `.claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py`

### Fix 2: BaoStock周线参数错误（D2）✅ 已修复

**问题**: BaoStock周线查询时传入了`preclose`字段但周线不支持。

**方案**: 修改 `fetch_baostock()` 函数，根据频率调整查询字段：

```python
if frequency == "w":
    fields = "date,open,high,low,close,volume,amount,pctChg"
    # 移除 preclose 字段（周线不支持）
else:
    fields = "date,open,high,low,close,volume,amount,pctChg,preclose"
```

**文件**: `.claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py` L324-331

### Fix 3: 小数据量警告与指标保护（A1/A2）✅ 已修复

**问题**: 数据不足时指标计算结果误导性很强，但summary中没有醒目标注。

**方案**: 在 `analyze_technical.py` 的 `build_summary()` 中：

1. 当 `data_points < 30` 时，在 summary 中增加 `data_quality: "insufficient"` 标记
2. 当 `30 <= data_points < 60` 时，增加 `data_quality: "limited"` 标记
3. insufficient_data 的指标不计入 consistency 分母
4. 在 key_signals 首位增加数据量警告

```python
# 在 build_summary 中
valid_scores = [s for s in scores if s != 0 or indicator_results.get(name, {}).get("signal", {}).get("type") != "insufficient_data"]
total_count = len(valid_scores) if valid_scores else 1

if data_points < 30:
    summary["data_quality"] = "insufficient"
    summary["key_signals"].insert(0, f"⚠️ 数据仅{data_points}条，分析结果可靠性极低")
elif data_points < 60:
    summary["data_quality"] = "limited"
    summary["key_signals"].insert(0, f"⚠️ 数据仅{data_points}条，部分指标可能不准确")
```

**文件**: `.claude/skills/stock-trend/scripts/analyze_technical.py`

### Fix 4: stdin 输入支持（A3）✅ 已修复

**问题**: `analyze_technical.py` 不支持 `-` 作为stdin。

**方案**: 修改 argparse 的 input_file 处理逻辑：

```python
if args.input_file and args.input_file != "-":
    with open(args.input_file, "r", encoding="utf-8") as f:
        input_data = json.load(f)
else:
    input_data = json.load(sys.stdin)
```

**文件**: `.claude/skills/stock-trend/scripts/analyze_technical.py` L1138-1142

### Fix 5: risk_reward None 值保护（A5）✅ 已修复

**问题**: 当 stop_loss 或 target 为 None 时，summary 中的 risk_reward_ratio 等字段为 None。

**方案**: 在 `calc_risk_reward()` 返回时确保所有字段有默认值：

```python
return {
    "stop_loss": stop_loss,
    "target": target,
    "risk_reward_ratio": rr_ratio,
    "risk": round(risk, 2) if risk else 0,
    "reward": round(reward, 2) if reward else 0,
    "position_sizing": position,
    "data_quality_warning": "支撑/压力位数据不足，止损/目标价仅供参考" if not support_prices and not resistance_prices else None,
}
```

并在 summary 构建时跳过 None 值。

**文件**: `.claude/skills/stock-trend/scripts/analyze_technical.py`

### Fix 6: SKILL.md 工作流补全（W1/W4）✅ 已修复

**问题**: 图表生成脚本未纳入流程；数据源降级指引不清晰。

**方案**: 更新 SKILL.md Step 3，增加图表生成命令和降级判断逻辑：

```bash
# 4. 生成K线图（可选，默认模式使用）
python3 .claude/skills/stock-trend/scripts/generate_chart_html.py /tmp/kline.json --technical /tmp/technical.json -o /tmp/chart_fragment.html
```

增加降级判断说明：
- 检查 JSON `meta.data_source` 字段
- `"error"` → 自动尝试下一级数据源
- 所有数据源失败 → 技术面按 0 分，标注"无数据源"

**文件**: `.claude/skills/stock-trend/SKILL.md`

### Fix 7: 报告目录自动创建（W2）✅ 已修复

**方案**: 在 SKILL.md Step 10 中增加目录创建说明，或在脚本中自动创建：

```python
import os
os.makedirs(os.path.dirname(output_path), exist_ok=True)
```

**文件**: `.claude/skills/stock-trend/SKILL.md` + 各脚本的 `_output()` 函数

---

## 三、测试用例设计

创建 `.claude/skills/stock-trend/scripts/test_stock_trend.py`：

### 测试分类

#### 1. 数据获取测试 (test_fetch)

| 用例ID | 描述 | 输入 | 预期 |
|--------|------|------|------|
| TF-01 | 上交所股票(茅台) | `600519.SH` | `data_source != "error"`, `record_count >= 60` |
| TF-02 | 深交所股票(平安) | `000001.SZ` | `data_source != "error"`, `record_count >= 60` |
| TF-03 | 创业板股票 | `300750.SZ` | `data_source != "error"`, `record_count >= 60` |
| TF-04 | 科创板股票 | `688981.SH` | `data_source != "error"`, `record_count >= 60` |
| TF-05 | 上交所ETF | `513180.SH` | `data_source != "error"` |
| TF-06 | 深交所ETF | `159919.SZ` | `data_source != "error"` |
| TF-07 | 港股(腾讯) | `00700.HK` | 有数据返回（Fix 1后）或明确的不支持提示 |
| TF-08 | 无效代码 | `999999.SH` | `data_source == "error"`, 错误信息明确 |
| TF-09 | 周线数据 | `600519.SH --freq W` | `data_source != "error"`, `record_count >= 30` |
| TF-10 | BaoStock降级 | mock东财失败 | 自动降级到BaoStock |
| TF-11 | 数据字段完整性 | 任意成功结果 | 包含 trade_date, open, high, low, close, vol |

#### 2. 技术分析测试 (test_analyze)

| 用例ID | 描述 | 输入 | 预期 |
|--------|------|------|------|
| TA-01 | 正常数据(200+条) | 茅台K线数据 | 所有指标有signal.score，summary有完整字段 |
| TA-02 | 边界数据(15条) | 小数据集 | `data_quality == "insufficient"`, key_signals含警告 |
| TA-03 | 边界数据(50条) | 中等数据集 | `data_quality == "limited"`, 关键指标有值 |
| TA-04 | 空数据 | `data_source=error` | summary.total_score=0, 有错误信息 |
| TA-05 | score范围 | 任意数据 | 所有signal.score在[-3, +3]范围内 |
| TA-06 | summary字段完整性 | 任意成功结果 | 包含total_score, direction, confidence, consistency, stop_loss, target |
| TA-07 | stop_loss有效性 | 正常数据 | stop_loss < current_close（看多时） |
| TA-08 | pattern去重 | 任意数据 | patterns列表中无同名形态 |

#### 3. 降级链测试 (test_fallback)

| 用例ID | 描述 | Mock | 预期 |
|--------|------|------|------|
| TD-01 | Tushare失败→东方财富 | Tushare返回error | 自动使用东方财富 |
| TD-02 | 东方财富失败→BaoStock | 东财所有节点失败 | 自动使用BaoStock |
| TD-03 | 全部失败 | Mock所有失败 | data_source=error，有明确错误 |
| TD-04 | 港股降级 | 港股代码 | 有明确的不支持或替代数据源提示 |

#### 4. 端到端测试 (test_e2e)

| 用例ID | 描述 | 命令 | 预期 |
|--------|------|------|------|
| TE-01 | 完整流程(上交所) | `/stock-trend 600519` | 生成MD和HTML报告，HTML可打开 |
| TE-02 | 完整流程(ETF) | `/stock-trend 513180` | 报告含IOPV折溢价分析 |
| TE-03 | 完整流程(港股) | `/stock-trend 00700` | 有数据或明确提示 |
| TE-04 | 精简模式 | `/stock-trend 600519 --compact` | 仅输出文本，不生成HTML |
| TE-05 | 多周期模式 | `/stock-trend 600519 --multi-timeframe` | 同时有日/周分析 |
| TE-06 | 无效代码 | `/stock-trend 999` | 友好错误提示 |

---

## 四、修改文件清单

| 文件 | 修改内容 |
|------|----------|
| `.claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py` | 增加港股数据源(Fix 1)；修复BaoStock周线参数(Fix 2) |
| `.claude/skills/stock-trend/scripts/analyze_technical.py` | 小数据保护(Fix 3)；stdin支持(Fix 4)；None值保护(Fix 5) |
| `.claude/skills/stock-trend/scripts/diagnose.py` | **新建**：诊断脚本 |
| `.claude/skills/stock-trend/scripts/test_stock_trend.py` | **新建**：测试脚本 |
| `.claude/skills/stock-trend/SKILL.md` | 增加诊断步骤、图表生成命令、降级逻辑说明(Fix 6) |

---

## 五、验证方式

1. 运行诊断脚本：`python3 diagnose.py` 确认所有数据源检测正常
2. 运行测试脚本：`python3 test_stock_trend.py -v` 全部通过
3. 端到端验证：`/stock-trend 600519` → `/stock-trend 00700` → `/stock-trend 513180`
4. 检查港股不再全链路失败
5. 检查小数据量时报告包含数据质量警告