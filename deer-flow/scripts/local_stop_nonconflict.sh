#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT_DIR/.run"

kill_pid_file() {
  local file="$1"
  if [[ -f "$file" ]]; then
    local pid
    pid="$(cat "$file")"
    kill "$pid" >/dev/null 2>&1 || true
    sleep 1
    kill -9 "$pid" >/dev/null 2>&1 || true
    rm -f "$file"
  fi
}

kill_pid_file "$RUN_DIR/nginx.pid"
kill_pid_file "$RUN_DIR/frontend.pid"
kill_pid_file "$RUN_DIR/gateway.pid"
kill_pid_file "$RUN_DIR/langgraph.pid"

pkill -f "langgraph dev --port 2124" >/dev/null 2>&1 || true
pkill -f "uvicorn app.gateway.app:app --host 127.0.0.1 --port 8101" >/dev/null 2>&1 || true
pkill -f "PORT=3101 .*pnpm run dev" >/dev/null 2>&1 || true

echo "stopped deer-flow local stack"
