#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-18900}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-18901}"
LOG_DIR="$ROOT_DIR/runtime_data/logs"
PID_DIR="$ROOT_DIR/runtime_data/pids"

mkdir -p "$LOG_DIR" "$PID_DIR"

free_port() {
  local port="$1"
  local pids
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "$pids" | xargs kill >/dev/null 2>&1 || true
  fi
}

start_if_needed() {
  local name="$1"
  local pid_file="$2"
  local port="$3"
  local cmd="$4"
  local log_file="$LOG_DIR/${name}.log"
  local service_pid=""
  local launcher_pid=""
  local i=0

  if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN -Fp 2>/dev/null | sed -n 's/^p//p' | grep -qx "$(cat "$pid_file")"; then
      echo "[$name] already running pid=$(cat "$pid_file")"
      return
    fi
  fi

  echo "[$name] starting..."
  nohup bash -lc "$cmd" >"$log_file" 2>&1 &
  launcher_pid="$!"

  # Wait until the service actually binds the target port; otherwise fail fast.
  while [ "$i" -lt 40 ]; do
    service_pid="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -Fp 2>/dev/null | sed -n 's/^p//p' | head -n 1 || true)"
    if [ -n "$service_pid" ]; then
      echo "$service_pid" >"$pid_file"
      echo "[$name] running pid=$service_pid"
      return
    fi
    if ! kill -0 "$launcher_pid" 2>/dev/null; then
      break
    fi
    sleep 0.25
    i=$((i + 1))
  done

  echo "[$name] failed to start on port $port"
  echo "[$name] recent logs:"
  tail -n 40 "$log_file" || true
  return 1
}

free_port "$BACKEND_PORT"
free_port "$FRONTEND_PORT"

start_if_needed "vibe_backend" "$PID_DIR/vibe_backend.pid" "$BACKEND_PORT" "cd '$ROOT_DIR' && HOST='$BACKEND_HOST' PORT='$BACKEND_PORT' bash scripts/start_vibe_backend.sh"
start_if_needed "vibe_frontend" "$PID_DIR/vibe_frontend.pid" "$FRONTEND_PORT" "cd '$ROOT_DIR' && HOST='$FRONTEND_HOST' PORT='$FRONTEND_PORT' bash scripts/start_vibe_frontend.sh"

echo "Backend:  http://${BACKEND_HOST}:${BACKEND_PORT}"
echo "Frontend: http://${FRONTEND_HOST}:${FRONTEND_PORT}"
echo "Logs:     $LOG_DIR"
