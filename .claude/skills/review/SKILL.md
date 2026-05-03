---
name: review
description: 对代码变更执行多维度审查（安全/性能/可读/健壮/最佳实践），输出结构化报告。当用户要求"审查代码"、"review my code"、"检查代码"、"review my changes"时自动触发。
argument-hint: "[scope:focus:severity]"
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash(git diff*)
  - Bash(git status*)
  - Bash(git log*)
  - Bash(git branch*)
  - Bash(git check-ignore*)
  - Bash(wc *)
  - Bash(file *)
---

# Code Review Skill

对代码变更执行多维度审查，输出结构化报告。

## 参数解析

从 `$ARGUMENTS` 提取参数，格式为空格分隔的 `key:value` 对：

```
/review scope:staged focus:security severity:critical
```

| 参数 | 可选值 | 默认值 | 说明 |
|------|--------|--------|------|
| scope | staged, unstaged, branch, \<filepath\> | staged | 审查范围 |
| focus | all, security, performance, style | all | 审查维度 |
| severity | critical, warning, info | warning | 最低报告级别 |

如果 `$ARGUMENTS` 为空，使用全部默认值。

## Step 1: 收集上下文

### 当前 Git 状态

- 变更文件：!`git diff --name-only`
- 暂存文件：!`git diff --cached --name-only`
- 当前分支：!`git branch --show-current`
- 最近提交：!`git log --oneline -5`

### 空变更检测

如果目标范围内的变更文件列表为空，输出 "No changes to review" 并停止。

## Step 2: 确定审查范围

根据 `scope` 参数获取变更：

| scope | 操作 |
|-------|------|
| staged | `git diff --cached --name-only` 获取文件列表，`git diff --cached` 获取差异 |
| unstaged | `git diff --name-only` 获取文件列表，`git diff` 获取差异 |
| branch | 检测默认分支名（main 或 master），`git diff <default>...HEAD --name-only` 获取文件列表 |
| \<filepath\> | `git diff HEAD -- <filepath>` 获取差异 |

### 范围过滤

1. 跳过生成目录中的文件：`node_modules/`、`dist/`、`vendor/`、`build/`、`__pycache__/`、`.next/`、`target/`
2. 用 `git diff --numstat` 检测二进制文件（显示为 `- - filename`），跳过并在 Info 中标注
3. 如果文件数超过 20，输出警告："More than 20 files changed. Consider narrowing scope for a thorough review."
4. 对超过 500 行的文件，只审查变更部分及周围上下文

## Step 3: 逐文件审查

对范围内每个文件：

1. 用 `git diff` 获取该文件的变更内容
2. 用 `git check-ignore` 检查是否在 `.gitignore` 中（是则标记为 Warning）
3. 用 `Read` 工具读取完整文件获取上下文（大文件只读取变更区域）
4. 根据 `focus` 参数选择审查维度：

| focus | 激活维度 |
|-------|---------|
| all | Security, Performance, Readability, Robustness, Best Practices |
| security | Security |
| performance | Performance |
| style | Readability, Best Practices |

5. 按 `references/review-dimensions.md` 中的标准逐项检查
6. 为每个发现记录：文件路径:行号、严重级别（Critical/Warning/Info）、简述、修复建议
7. 遇到无法解析的语法错误，标记为 Critical 并跳过该文件的深入审查

## Step 4: 生成报告

按 `assets/report-template.md` 格式输出报告：

1. 汇总 Findings，按 severity 排序：Critical > Warning > Info
2. 按 `severity` 参数过滤：`critical` 只显示 Critical，`warning` 显示 Critical + Warning，`info` 显示全部
3. 确定整体状态：
   - **PASS**: 无达到阈值的 Findings
   - **WARNINGS**: 有 Warning 但无 Critical
   - **ISSUES FOUND**: 有至少 1 个 Critical
4. 计算各维度评分（1-5）：
   - 5 = 无问题
   - 4 = 轻微建议
   - 3 = 有改进建议
   - 2 = 存在问题应修复
   - 1 = 严重问题必须修复
   - 单个 Critical 找到 → 该维度上限 2 分
5. 使用 `assets/report-template.md` 的格式输出最终报告

## 约束

- **不修改任何代码** — 只报告问题，不自动修复（除非用户明确要求）
- 语言优先级：TypeScript/JavaScript > Python > Go，其他语言提供基本审查
- 报告语言跟随用户输入语言（中文提问 → 中文报告，英文提问 → 英文报告）