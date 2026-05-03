---
name: code-review-skill
description: 代码审查 Agent Skill 的功能规格说明
type: project
---

# Code Review Skill - 规格说明

## Goal

构建一个 Claude Code Skill，在用户提交代码变更前自动执行多维度审查，输出结构化审查报告，帮助开发者提前发现质量问题。

## 触发条件

- 用户输入 `/review` 命令时触发
- 或用户说"审查代码"、"review my code"等语义等价指令

## 输入

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| scope | string | 否 | 审查范围：`staged`(默认)、`unstaged`、`branch`、指定文件路径 |
| focus | string | 否 | 审查重点：`security`、`performance`、`style`、`all`(默认) |
| severity | string | 否 | 最低报告级别：`critical`、`warning`(默认)、`info` |

## 输出

输出 Markdown 格式的审查报告，包含以下结构：

```markdown
## Code Review Report

**Scope**: [审查范围]
**Files**: [文件数量]
**Status**: ✅ PASS / ⚠️ WARNINGS / ❌ ISSUES FOUND

### Summary
[1-2 句话总结]

### Findings

#### 🔴 Critical (N)
- **[file:line]** 简述 — 修复建议

#### 🟡 Warning (N)
- **[file:line]** 简述 — 修复建议

#### 🔵 Info (N)
- **[file:line]** 简述

### Metrics
| 维度 | 评分(1-5) | 说明 |
|------|----------|------|
| 安全性 | X | ... |
| 可读性 | X | ... |
| 性能 | X | ... |
| 健壮性 | X | ... |
```

## 审查维度

### 1. 安全性 (Security)
- SQL 注入、XSS、命令注入等 OWASP Top 10
- 硬编码密钥、token、密码
- 不安全的依赖函数调用（eval、exec 等）
- 敏感数据泄露（日志中的密码、个人信息）

### 2. 性能 (Performance)
- N+1 查询
- 不必要的全量数据加载
- 缺少缓存机会
- 循环内的重复计算

### 3. 可读性 (Readability)
- 命名不清晰（单字母变量、模糊函数名）
- 函数过长（超过 50 行）
- 嵌套过深（超过 3 层）
- 缺少必要的注释（WHY 级别）

### 4. 健壮性 (Robustness)
- 缺少错误处理
- 未处理的边界情况
- 类型不安全
- 资源泄露（未关闭的连接、文件句柄）

### 5. 最佳实践 (Best Practices)
- 违反 DRY 原则
- 不符合项目既定模式
- 缺少必要的测试

## 约束

- **不修改代码**：Skill 只报告问题，不自动修复（除非用户明确要求）
- **不审查生成的代码**：跳过 `node_modules/`、`dist/`、`vendor/` 等生成目录
- **单文件上限**：超过 500 行的文件只审查变更部分，不审查全文件
- **语言支持**：TypeScript/JavaScript、Python、Go 为主，其他语言提供基本审查
- **性能要求**：审查响应时间 < 30 秒（对于 10 个文件以内的变更）

## Edge Cases

| 情况 | 处理方式 |
|------|----------|
| 没有变更文件 | 输出 "No changes to review" 并退出 |
| 二进制文件变更 | 跳过，在 Info 中标注 |
| 变更量超过 20 个文件 | 警告用户，建议缩小范围 |
| 无法解析的语法错误 | 标记为 Critical，跳过该文件其他审查 |
| `.gitignore` 中的文件被修改 | 标记为 Warning，可能不应提交 |

## 示例

### 输入
```
/review scope:staged focus:security
```

### 输出
```markdown
## Code Review Report

**Scope**: staged changes
**Files**: 3
**Status**: ⚠️ WARNINGS

### Summary
发现 1 个安全警告和 2 个代码质量问题。建议修复硬编码密钥后再提交。

### Findings

#### 🔴 Critical (0)

#### 🟡 Warning (1)
- **src/auth.ts:42** 硬编码 API Key — 使用环境变量替换：`process.env.API_KEY`

#### 🔵 Info (2)
- **src/utils.ts:15** 函数 `formatDate` 超过 50 行，考虑拆分
- **src/index.ts:8** 未使用的 import `lodash`

### Metrics
| 维度 | 评分(1-5) | 说明 |
|------|----------|------|
| 安全性 | 3 | 存在硬编码密钥 |
| 可读性 | 4 | 命名清晰，少量冗余 |
| 性能 | 4 | 无明显问题 |
| 健壮性 | 3 | 缺少 API 调用的错误处理 |
```

## 技术实现要点

- Skill 入口文件：`.claude/skills/code-review.md`（Skill 定义文件）
- 使用 `git diff` 获取变更内容
- 按文件逐一审查，汇总报告
- 审查结果按 severity 排序：Critical > Warning > Info

---

**Why**: 结构化 Spec 减少 Agent 的猜测和返工，让审查结果一致且可预期。
**How to apply**: 每次 Agent 执行 `/review` 时，严格按此 Spec 输出格式和审查维度执行。