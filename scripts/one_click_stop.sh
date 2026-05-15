#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

echo "[1/2] stop vibe stack"
"$ROOT_DIR/scripts/stop_vibe_stack.sh" || true

echo "[2/2] stop legacy demo/mapping daemons"
"$ROOT_DIR/scripts/stop_demo_remote_daemon.sh" || true
"$ROOT_DIR/scripts/stop_remote_mapping_daemon.sh" || true
pkill -f "run_demo.py" >/dev/null 2>&1 || true

echo "Stopped"
