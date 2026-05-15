#!/bin/bash
# Install git hooks for stock-trend project
# Run: bash .githooks/install-hooks.sh

HOOKS_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🔧 Installing git hooks from $HOOKS_DIR..."
git config core.hooksPath "$HOOKS_DIR"
echo ""
echo "✅  pre-commit hook 已安装"
echo "    hooksPath → $HOOKS_DIR"
echo ""
echo "现在每次 git commit 前会自动执行检查。"
echo "紧急情况跳过: git commit --no-verify"
