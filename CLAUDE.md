# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Code Review Skill — Claude Code 的代码审查技能插件，在提交前自动执行多维度审查。

## Specs

- [Code Review Skill Spec](.claude/specs/code-review-skill.md) — 功能规格说明，定义输入/输出、审查维度、约束和示例

## Commands

- Review staged changes: `/review`
- Review specific scope: `/review scope:staged focus:security`
- Review full branch diff: `/review scope:branch`

## Architecture

```
.claude/
├── skills/
│   └── review/
│       ├── SKILL.md                      # Skill 定义入口（YAML frontmatter + 工作流）
│       ├── references/
│       │   └── review-dimensions.md       # 五个审查维度的详细检查标准
│       └── assets/
│           └── report-template.md         # 输出报告模板
└── specs/
    └── code-review-skill.md              # 功能规格说明
```

- `/review` → `.claude/skills/review/SKILL.md`
- 审查标准 → `.claude/skills/review/references/review-dimensions.md`
- 报告格式 → `.claude/skills/review/assets/report-template.md`
- 规格说明 → `.claude/specs/code-review-skill.md`