# 修改代码工作流设计：防止分析数据劣化

日期：2026-05-16

## 问题

修改 stock-trend 脚本时，可能出现两类数据劣化：
1. **格式变化未被发现**：输出 JSON 增删字段、类型变化，导致下游消费方（compute_scores / generate_report）崩溃或静默产出错误结果
2. **质量退化无声发生**：缓存策略、数据源、计算逻辑变化后，报告看起来"正常"实则数据不准

## 方案

**Golden Snapshot + Diff + 轻量 Schema 校验**

### 一、Golden Snapshot 机制

**目录结构**：

```
.claude/skills/stock-trend/tests/
├── golden/                          # Golden output 文件
│   ├── 600519.SH/                   # A股标的
│   │   ├── resolve.json
│   │   ├── kline.json
│   │   ├── technical.json
│   │   ├── capital_flow.json
│   │   ├── fundamental.json
│   │   ├── macro_snapshot.json
│   │   ├── scores.json
│   │   └── pipeline_output.json
│   ├── 513180.SH/                   # ETF 标的
│   │   ├── resolve.json
│   │   ├── kline.json
│   │   ├── ...
│   │   └── etf_data.json
│   └── 00700.HK/                    # 港股标的
│       └── ...
├── fixtures/                        # Mock 输入（固定 K线数据等）
│   ├── kline_600519.SH.json
│   └── ...
└── test_golden.py                   # Snapshot 生成 & diff 工具
```

**覆盖 3 种资产类型**：A股(600519)、ETF(513180)、港股(00700)

**fixtures**：固定输入数据，脚本通过 `--stdin` 或 mock 加载，避免依赖实时 API

**Diff 规则**：

| 变化类型 | 处理方式 |
|----------|----------|
| 结构变化（增删字段、类型变化） | 严格失败 |
| 列表长度变化 | 严格失败 |
| 数值变化 | 超阈值失败，阈值内警告 |
| 列表内数值 | 逐元素 diff，同阈值规则 |

**阈值默认值**：分数类 ±0.01，价格类 ±0.0001（可在 golden config 中调整）

**命令**：
- `python3 tests/test_golden.py --diff` — 比对当前输出与 golden
- `python3 tests/test_golden.py --regenerate` — 重新生成 golden（需在 commit message 说明原因）

### 二、轻量 Schema 校验

**位置**：`compute_scores.py` 入口处

**校验项**：

| 校验项 | 规则 |
|--------|------|
| technical.json 必需字段 | `summary`, `summary.total_score`, `summary.direction`, `summary.confidence`, `data_quality` 存在且类型正确 |
| 各维度数据存在性 | 文件存在 + 非空 + JSON 可解析 |
| 分数范围 | 所有 `*_score` 参数值在 [-100, 100] |
| 类型校验 | score 为数值，summary 为字符串，data_quality 为枚举值 |

**原则**：
- 不建独立 schema 文件，直接在代码里 assert
- 校验失败时输出明确错误信息，不静默跳过
- 上游脚本的输出格式由 golden snapshot 覆盖，不在每个脚本内加 schema

### 三、Claude Code 层工作流约束

写入 CLAUDE.md：

```
## 修改代码工作流

修改 .claude/skills/stock-trend/scripts/ 下任何 .py 文件时：

1. Plan: 说明改什么、影响范围
2. Execute: 做修改
3. Test: 必须执行以下步骤
   a. python3 test_stock_trend.py           # 现有测试全过
   b. python3 tests/test_golden.py --diff   # Golden snapshot diff 无失败
   c. 如果 diff 有数值变化但合理：用 --regenerate 更新 golden，写明原因
4. Commit: 确认 3a+3b 通过后再提交
```

**关键约束**：
- 步骤 3 不可跳过
- 合理的 golden 变化必须 `--regenerate` 并在 commit message 说明
- 规则写入 CLAUDE.md，Claude Code 每次对话读取

### 四、CI 兜底（pre-commit hook）

扩展现有 `.githooks/pre-commit`，新增：

```bash
5. Golden snapshot diff 检查
   - 如果 scripts/ 下有 .py 文件被修改：
     - 运行 python3 tests/test_golden.py --diff
     - 有 FAIL 输出则拒绝提交
   - 跳过条件：commit message 含 [skip-golden]

6. compute_scores schema 校验
   - 如果 compute_scores.py 被修改：
     - 运行 python3 -c "from compute_scores import validate_input; ..."
     - 校验不通过则拒绝提交
```

**设计原则**：
- pre-commit hook 不跑全量数据采集（太慢），只跑基于 fixtures 的 diff
- golden 文件本身修改时不触发 diff 检查
- `[skip-golden]` 仅用于紧急修复，不鼓励常规使用

## 实现优先级

1. `tests/test_golden.py` — Snapshot 生成 & diff 工具（核心）
2. `tests/fixtures/` — 固定输入数据
3. `tests/golden/` — 3 个标的的 golden output
4. `compute_scores.py` 的 `validate_input()` — Schema 校验
5. CLAUDE.md 工作流规则更新
6. `.githooks/pre-commit` 扩展
7. `test_stock_trend.py` 中增加 golden diff 测试用例

## 不在范围内

- 不建独立 schema 定义文件（如 JSON Schema）
- 不在每个上游脚本加 schema 校验
- 不引入 pytest 框架（扩展现有自定义框架）
- 不做 GitHub Actions CI（只用 pre-commit hook）