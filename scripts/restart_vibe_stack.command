#!/usr/bin/env bash
set -euo pipefail

DEFAULT_ROOT_DIR="/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${FZ_WORKFLOW_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

if [ ! -f "$ROOT_DIR/scripts/start_vibe_stack.sh" ]; then
  ROOT_DIR="$DEFAULT_ROOT_DIR"
fi

if [ ! -f "$ROOT_DIR/scripts/start_vibe_stack.sh" ] || [ ! -f "$ROOT_DIR/scripts/stop_vibe_stack.sh" ]; then
  echo "Cannot find fz_workflow scripts under: $ROOT_DIR"
  exit 1
fi

export PATH="${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-18900}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-18901}"
BACKEND_HEALTH_URL="http://${BACKEND_HOST}:${BACKEND_PORT}/health"
FRONTEND_URL="http://${FRONTEND_HOST}:${FRONTEND_PORT}/"
LOG_DIR="$ROOT_DIR/runtime_data/logs"
AGENT_ID="com.fzworkflow.local-vibe"
PLIST_PATH="$HOME/Library/LaunchAgents/${AGENT_ID}.plist"

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempt=0

  while [ "$attempt" -lt 60 ]; do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      echo "[$label] ready: $url"
      return 0
    fi
    sleep 0.5
    attempt=$((attempt + 1))
  done

  echo "[$label] timeout: $url"
  return 1
}

show_recent_logs() {
  echo
  echo "Recent backend log:"
  tail -n 80 "$LOG_DIR/vibe_backend.log" 2>/dev/null || true
  echo
  echo "Recent frontend log:"
  tail -n 40 "$LOG_DIR/vibe_frontend.log" 2>/dev/null || true
}

open_browser() {
  if command -v open >/dev/null 2>&1; then
    open "$FRONTEND_URL" >/dev/null 2>&1 || true
  fi
}

cleanup_old_launch_agent() {
  launchctl bootout "gui/$(id -u)/${AGENT_ID}" >/dev/null 2>&1 || true
  launchctl remove "${AGENT_ID}" >/dev/null 2>&1 || true
  rm -f "$PLIST_PATH"
}

cd "$ROOT_DIR"
mkdir -p "$LOG_DIR" "$ROOT_DIR/runtime_data/pids"

echo "[1/5] cleanup old LaunchAgent"
cleanup_old_launch_agent

echo "[2/5] stop current stack"
bash scripts/stop_vibe_stack.sh || true

echo "[3/5] start stack"
bash scripts/start_vibe_stack.sh

echo "[4/5] verify"
if ! wait_for_url "$BACKEND_HEALTH_URL" "backend"; then
  show_recent_logs
  exit 1
fi
if ! wait_for_url "$FRONTEND_URL" "frontend"; then
  show_recent_logs
  exit 1
fi

echo "[5/5] open browser"
open_browser

echo
echo "Backend:  $BACKEND_HEALTH_URL"
echo "Frontend: $FRONTEND_URL"
echo "Logs:     $LOG_DIR"

if [ -t 0 ]; then
  echo
  read -r -p "Press Enter to close this window..." _
fi
