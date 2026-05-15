#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

VIBE_URL="http://127.0.0.1:18901/"
VIBE_BACKEND_HEALTH_URL="http://127.0.0.1:18900/health"
DEERFLOW_URL="http://127.0.0.1:2126/workspace/chats/fe3f7974-1bcb-4a01-a950-79673baafefd"

usage() {
  cat <<USAGE
Usage: bash scripts/control_pages.sh <start|stop|status|restart>

Targets:
- ${VIBE_URL}
- ${DEERFLOW_URL}
USAGE
}

url_up() {
  local url="$1"
  curl -fsS --max-time 3 "$url" >/dev/null 2>&1
}

status_line() {
  local name="$1"
  local url="$2"
  if url_up "$url"; then
    echo "[UP]   ${name}: ${url}"
  else
    echo "[DOWN] ${name}: ${url}"
  fi
}

status_vibe() {
  local frontend_ok=0
  local backend_ok=0
  url_up "$VIBE_URL" && frontend_ok=1
  url_up "$VIBE_BACKEND_HEALTH_URL" && backend_ok=1

  if [ "$frontend_ok" -eq 1 ] && [ "$backend_ok" -eq 1 ]; then
    echo "[UP]   vibe: ${VIBE_URL} (backend: ${VIBE_BACKEND_HEALTH_URL})"
    return
  fi

  if [ "$frontend_ok" -eq 1 ] && [ "$backend_ok" -eq 0 ]; then
    echo "[DOWN] vibe backend: ${VIBE_BACKEND_HEALTH_URL} (frontend up: ${VIBE_URL})"
    return
  fi

  if [ "$frontend_ok" -eq 0 ] && [ "$backend_ok" -eq 1 ]; then
    echo "[DOWN] vibe frontend: ${VIBE_URL} (backend up: ${VIBE_BACKEND_HEALTH_URL})"
    return
  fi

  echo "[DOWN] vibe: ${VIBE_URL} (backend: ${VIBE_BACKEND_HEALTH_URL})"
}

start_vibe() {
  if url_up "$VIBE_URL"; then
    echo "[skip] vibe already up"
    return
  fi
  bash "$ROOT_DIR/scripts/start_vibe_stack.sh"
}

start_deerflow() {
  if url_up "$DEERFLOW_URL"; then
    echo "[skip] deer-flow already up"
    return
  fi
  bash "$ROOT_DIR/scripts/deer_flow_one_click_start.sh"
}

stop_all() {
  bash "$ROOT_DIR/scripts/deer_flow_stop.sh" || true
  bash "$ROOT_DIR/scripts/stop_vibe_stack.sh" || true
  bash "$ROOT_DIR/scripts/stop_demo_remote_daemon.sh" || true
  bash "$ROOT_DIR/scripts/stop_remote_mapping_daemon.sh" || true
  pkill -f "run_demo.py" >/dev/null 2>&1 || true
}

print_target_urls() {
  echo "Targets:"
  echo "- ${VIBE_URL}"
  echo "- ${DEERFLOW_URL}"
}

start_all() {
  print_target_urls
  echo
  start_vibe
  start_deerflow
  echo
  echo "Startup status:"
  status_all
  echo
  echo "Open in browser:"
  echo "${VIBE_URL}"
  echo "${DEERFLOW_URL}"
}

status_all() {
  status_vibe
  status_line "deer-flow" "$DEERFLOW_URL"
}

cmd="${1:-}"
case "$cmd" in
  start)
    start_all
    ;;
  stop)
    stop_all
    echo
    status_all
    ;;
  status)
    status_all
    ;;
  restart)
    stop_all
    start_all
    ;;
  *)
    usage
    exit 1
    ;;
esac
