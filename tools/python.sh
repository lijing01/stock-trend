#!/bin/sh

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
venv_dir="$repo_root/.venv"

fail() {
    printf 'stock-trend: %s\n' "$*" >&2
    exit 1
}

resolve_command() {
    requested=$1
    resolved=$(command -v "$requested" 2>/dev/null) || return 1
    [ -x "$resolved" ] || return 1
    printf '%s\n' "$resolved"
}

if [ "${STOCK_TREND_PYTHON+x}" = x ]; then
    [ -n "$STOCK_TREND_PYTHON" ] || \
        fail 'STOCK_TREND_PYTHON is set but empty'
    python_bin=$(resolve_command "$STOCK_TREND_PYTHON") || \
        fail "STOCK_TREND_PYTHON is not an executable path or command: $STOCK_TREND_PYTHON"
elif [ -d "$venv_dir" ] || [ -e "$venv_dir" ] || [ -L "$venv_dir" ]; then
    python_bin="$venv_dir/bin/python"
    [ -x "$python_bin" ] || \
        fail "repository virtual environment exists but its Python is not executable: $python_bin"
else
    python_bin=$(resolve_command python3) || \
        fail 'python3 was not found; create .venv or set STOCK_TREND_PYTHON'
fi

version=$(
    "$python_bin" -c \
        'import sys; print(".".join(str(part) for part in sys.version_info[:3])); raise SystemExit(sys.version_info < (3, 10))'
) || fail "Python 3.10 or newer is required (selected: $python_bin${version:+, version: $version})"

printf 'stock-trend: using Python interpreter: %s\n' "$python_bin" >&2
printf 'stock-trend: Python version: %s\n' "$version" >&2

exec "$python_bin" "$@"
