#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOST_OVERRIDE="${HOST:-}"
PORT_OVERRIDE="${PORT:-}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/fz-workflow-demo.app.env}"

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a
  . "$ENV_FILE"
  set +a
fi

HOST="${HOST_OVERRIDE:-${HOST:-127.0.0.1}}"
PORT="${PORT_OVERRIDE:-${PORT:-18900}}"

cd "$ROOT_DIR"
export VIBE_PERSIST_STATE="${VIBE_PERSIST_STATE:-1}"
# ⚠️ 必须显式摘掉 HTTP_PROXY/HTTPS_PROXY：启动环境（含沙箱/会话）常注入指向**本机临时端口**的代理变量，
# 那个代理进程一退出，本进程所有出网调用就一起以 ECONNREFUSED 静默失败。
# 实测症状：模型 `/probe` 与画布取数同时报
# `sql-result http provider failed: <urlopen error [Errno 61] Connection refused>`，
# 而同一台机器上 curl 直连 api.deepseek.com 完全正常 —— 极易误判成"模型或 API 挂了"。
# 本后端只访问内网网关、DeepSeek 官方 API 与本机 MySQL，永远不需要代理。
exec env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  "$PYTHON_BIN" -m uvicorn apps.vibe_backend.app.main:app --host "$HOST" --port "$PORT"
