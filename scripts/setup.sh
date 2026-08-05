#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -m venv "$PROJECT_ROOT/.venv"
"$PROJECT_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$PROJECT_ROOT/.venv/bin/python" -m pip install -e "$PROJECT_ROOT[crawl,ocr]"
"$PROJECT_ROOT/.venv/bin/agent-reach" install --channels opencli

if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
fi

echo
echo "安装完成。请在自己的 Chrome 登录小红书，然后运行："
echo "  $PROJECT_ROOT/.venv/bin/agent-reach doctor --json"
echo "启动看板：bash scripts/start.sh"
