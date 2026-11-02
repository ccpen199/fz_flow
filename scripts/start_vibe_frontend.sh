#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-18901}"

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

cd "$ROOT_DIR"
# 用自带缓存策略的静态服务（HTML 必须回源校验）。原因见 serve_vibe_frontend.py 顶部注释：
# python -m http.server 不带 Cache-Control，浏览器会启发式缓存 agent.html，
# 于是"改了 agent.js + 升了 ?v= 缓存戳"用户却仍看到旧页面。
exec "$PYTHON_BIN" "$ROOT_DIR/scripts/serve_vibe_frontend.py" \
  --bind "$HOST" --port "$PORT" --directory "$ROOT_DIR/apps/vibe_frontend/static"
