#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOST_OVERRIDE="${HOST:-}"
PORT_OVERRIDE="${PORT:-}"
PPTX_EXPORTER_OVERRIDE="${VIBE_PPTX_EXPORTER:-}"
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
VIBE_PPTX_EXPORTER="${PPTX_EXPORTER_OVERRIDE:-${VIBE_PPTX_EXPORTER:-python-pptx}}"

cd "$ROOT_DIR"
export VIBE_PERSIST_STATE="${VIBE_PERSIST_STATE:-1}"
export VIBE_PPTX_EXPORTER
export PRESENTON_BASE_URL="${PRESENTON_BASE_URL:-http://127.0.0.1:${PRESENTON_FASTAPI_PORT:-15001}}"
export PRESENTON_APP_DATA_DIR="${PRESENTON_APP_DATA_DIR:-$ROOT_DIR/runtime_data/presenton/app_data}"
export VIBE_PRESENTON_ALLOW_FALLBACK="${VIBE_PRESENTON_ALLOW_FALLBACK:-0}"
export PRESENTON_DIRECT_DATA_SLIDES="${PRESENTON_DIRECT_DATA_SLIDES:-1}"
export PRESENTON_APPEND_DATA_SLIDES="${PRESENTON_APPEND_DATA_SLIDES:-0}"
exec "$PYTHON_BIN" -m uvicorn apps.vibe_backend.app.main:app --host "$HOST" --port "$PORT"
