#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-fastapi}"
FASTAPI_PORT="${PRESENTON_FASTAPI_PORT:-15001}"
NEXTJS_PORT="${PRESENTON_NEXTJS_PORT:-15002}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/fz-workflow-demo.app.env}"

if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a
  . "$ENV_FILE"
  set +a
fi

HOST="${PRESENTON_HOST:-127.0.0.1}"

endpoint="${LLM_AGENT_HTTP_ENDPOINT:-}"
if [ -n "$endpoint" ]; then
  endpoint="${endpoint%/chat/completions}"
fi

export APP_DATA_DIRECTORY="${PRESENTON_APP_DATA_DIR:-$ROOT_DIR/runtime_data/presenton/app_data}"
export TEMP_DIRECTORY="${PRESENTON_TEMP_DIR:-$ROOT_DIR/runtime_data/presenton/tmp}"
export USER_CONFIG_PATH="${PRESENTON_USER_CONFIG_PATH:-$APP_DATA_DIRECTORY/userConfig.json}"
export FASTAPI_PUBLIC_URL="http://$HOST:$FASTAPI_PORT"
export NEXT_PUBLIC_FAST_API="$FASTAPI_PUBLIC_URL"
export FAST_API_INTERNAL_URL="$FASTAPI_PUBLIC_URL"
export NEXT_PUBLIC_URL="http://$HOST:$NEXTJS_PORT"
export EXPORT_RUNTIME_DIR="${EXPORT_RUNTIME_DIR:-$ROOT_DIR/third_party/presenton/presentation-export}"
export LITEPARSE_NODE_BINARY="${LITEPARSE_NODE_BINARY:-$(command -v node)}"
export DISABLE_AUTH="${DISABLE_AUTH:-true}"
export CAN_CHANGE_KEYS="${CAN_CHANGE_KEYS:-true}"
export DISABLE_IMAGE_GENERATION="${DISABLE_IMAGE_GENERATION:-true}"
export MEM0_ENABLED="${MEM0_ENABLED:-false}"
export MIGRATE_DATABASE_ON_STARTUP="${MIGRATE_DATABASE_ON_STARTUP:-false}"
export PRESENTON_FORCE_LAYOUT_ID="${PRESENTON_FORCE_LAYOUT_ID:-}"
export LLM="${LLM:-custom}"
export CUSTOM_LLM_URL="${CUSTOM_LLM_URL:-$endpoint}"
export CUSTOM_LLM_API_KEY="${CUSTOM_LLM_API_KEY:-${LLM_AGENT_API_KEY:-}}"
export CUSTOM_MODEL="${CUSTOM_MODEL:-${LLM_AGENT_HTTP_MODEL:-}}"

mkdir -p "$APP_DATA_DIRECTORY" "$TEMP_DIRECTORY"

case "$MODE" in
  fastapi)
    cd "$ROOT_DIR/third_party/presenton/servers/fastapi"
    exec .venv/bin/python server.py --port "$FASTAPI_PORT"
    ;;
  nextjs)
    cd "$ROOT_DIR/third_party/presenton/servers/nextjs"
    exec npm run dev -- -H "$HOST" -p "$NEXTJS_PORT"
    ;;
  *)
    echo "usage: $0 [fastapi|nextjs]" >&2
    exit 2
    ;;
esac
