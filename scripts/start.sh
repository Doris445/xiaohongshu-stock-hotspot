#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  echo "未找到 .venv，请先运行 scripts/setup.sh。" >&2
  exit 1
fi
exec "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/server.py" "$@"
