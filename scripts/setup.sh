#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_ROOT="$PROJECT_ROOT/.venv"
SKIP_XIAOHONGSHU="${SENTIBOARD_SKIP_XIAOHONGSHU:-0}"
WITH_SCRAPLING="${SENTIBOARD_WITH_SCRAPLING:-0}"

step() { printf '\n[%s/6] %s\n' "$1" "$2"; }

find_python() {
  local candidate
  if [[ -n "${SENTIBOARD_PYTHON:-}" ]]; then
    candidate="$SENTIBOARD_PYTHON"
    if "$candidate" -c 'import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] < (3,14) and sys.maxsize > 2**32 else 1)' 2>/dev/null; then
      printf '%s' "$candidate"
      return 0
    fi
  fi
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] < (3,14) and sys.maxsize > 2**32 else 1)' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

step 1 '检查 64 位 Python 3.10–3.13'
PYTHON="$(find_python || true)"
if [[ -z "$PYTHON" ]]; then
  echo '未找到兼容 Python。请先安装 64 位 Python 3.10–3.13。' >&2
  exit 2
fi
"$PYTHON" --version

step 2 '创建或复用项目虚拟环境'
if [[ ! -x "$VENV_ROOT/bin/python" ]]; then
  "$PYTHON" -m venv "$VENV_ROOT"
fi
VENV_PYTHON="$VENV_ROOT/bin/python"
PIP_ARGS=(-m pip install --disable-pip-version-check --no-input --prefer-binary --retries 3 --timeout 60)

step 3 '安装看板核心（固定版本，可重复运行并复用下载缓存）'
"$VENV_PYTHON" "${PIP_ARGS[@]}" -e "$PROJECT_ROOT"

step 4 '安装数据连接器'
if [[ "$SKIP_XIAOHONGSHU" != '1' ]]; then
  "$VENV_PYTHON" "${PIP_ARGS[@]}" -e "$PROJECT_ROOT[xiaohongshu]"
  "$VENV_ROOT/bin/agent-reach" install --env=auto --channels=xiaohongshu
else
  echo '已跳过小红书连接器；东方财富看板和演示数据仍可使用。'
fi
if [[ "$WITH_SCRAPLING" == '1' ]]; then
  echo '安装可选 Scrapling HTTP 加速层（此步骤依赖较多）...'
  "$VENV_PYTHON" "${PIP_ARGS[@]}" -e "$PROJECT_ROOT[scrapling]"
fi
if [[ "$(uname -s)" != MINGW* && "$(uname -s)" != CYGWIN* ]]; then
  echo '安装跨平台 OCR...'
  "$VENV_PYTHON" "${PIP_ARGS[@]}" -e "$PROJECT_ROOT[ocr]"
fi

step 5 '创建本机配置（不会覆盖已有 .env）'
if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
fi

step 6 '运行部署自检'
PATH="$VENV_ROOT/bin:$PATH" "$VENV_PYTHON" "$PROJECT_ROOT/scripts/doctor.py"

echo
echo '安装完成。请在自己的 Chrome 登录小红书；首次使用东方财富正文时按页面提示人工完成一次滑块。'
echo '启动看板：bash scripts/start.sh'
echo '如安装中断，直接重新运行本脚本即可从 pip 缓存继续，不要删除 .venv。'
