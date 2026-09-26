#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(git -C "$script_dir" rev-parse --show-toplevel)
cd "$repo_root"
existing=$(git config --get core.hooksPath || true)
case "$existing" in
    ''|.githooks|"$repo_root/.githooks") ;;
    *) printf '已有 hooksPath=%s；拒绝覆盖现有 hooks。\n' "$existing" >&2; exit 1 ;;
esac
test -x .githooks/pre-commit
test -f tools/python.sh
test -f tools/check_staged.py
git config --local core.hooksPath .githooks
test "$(git config --get core.hooksPath)" = .githooks
printf 'pre-commit 已安装：hooksPath → .githooks\n'
