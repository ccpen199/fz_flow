#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT_NAME="$(basename "$ROOT_DIR")"
PARENT_DIR="$(dirname "$ROOT_DIR")"
DATE_TAG="${DATE_TAG:-$(date +%F)}"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/handoff}"
OUT_FILE="$OUT_DIR/${ROOT_NAME}_customer_handoff_${DATE_TAG}.tar.gz"
MANIFEST_FILE="$OUT_DIR/${ROOT_NAME}_customer_handoff_${DATE_TAG}_manifest.txt"

mkdir -p "$OUT_DIR"

if [ ! -f "$ROOT_DIR/docs/Sql/dataservice_test_local_current_2026-05-15.sql" ]; then
  echo "missing current database dump: docs/Sql/dataservice_test_local_current_2026-05-15.sql" >&2
  exit 1
fi

rm -f "$OUT_FILE" "$MANIFEST_FILE"

tar -czf "$OUT_FILE" \
  --exclude="$ROOT_NAME/.DS_Store" \
  --exclude="*/.DS_Store" \
  --exclude="$ROOT_NAME/.git" \
  --exclude="$ROOT_NAME/.venv" \
  --exclude="$ROOT_NAME/.pytest_cache" \
  --exclude="$ROOT_NAME/handoff" \
  --exclude="$ROOT_NAME/apps/vibe_backend/.venv" \
  --exclude="$ROOT_NAME/deploy/fz-workflow-demo.app.env" \
  --exclude="$ROOT_NAME/deploy/fz-workflow-demo.conf.sh" \
  --exclude="$ROOT_NAME/data/*remote*" \
  --exclude="$ROOT_NAME/data/*tunnel*" \
  --exclude="$ROOT_NAME/runtime_data/logs" \
  --exclude="$ROOT_NAME/runtime_data/pids" \
  --exclude="$ROOT_NAME/runtime_data/presenton/tmp" \
  --exclude="$ROOT_NAME/runtime_data/presenton/app_data/userConfig.json" \
  --exclude="$ROOT_NAME/runtime_data/presenton/app_data/*.db" \
  --exclude="$ROOT_NAME/runtime_data/presenton/app_data/fastembed_cache" \
  --exclude="$ROOT_NAME/scripts/install_jdcloud_reverse_tunnel_launch_agent.sh" \
  --exclude="$ROOT_NAME/scripts/*remote*" \
  --exclude="$ROOT_NAME/scripts/*tunnel*" \
  --exclude="$ROOT_NAME/scripts/fz-*watchdog.sh" \
  --exclude="$ROOT_NAME/deer-flow/.git" \
  --exclude="$ROOT_NAME/deer-flow/.env" \
  --exclude="$ROOT_NAME/deer-flow/logs" \
  --exclude="$ROOT_NAME/deer-flow/backend/.venv" \
  --exclude="$ROOT_NAME/deer-flow/backend/.pytest_cache" \
  --exclude="$ROOT_NAME/deer-flow/frontend/.env" \
  --exclude="$ROOT_NAME/deer-flow/frontend/node_modules" \
  --exclude="$ROOT_NAME/deer-flow/frontend/.next" \
  --exclude="$ROOT_NAME/third_party/presenton/.git" \
  --exclude="$ROOT_NAME/third_party/presenton/.cache" \
  --exclude="$ROOT_NAME/third_party/presenton/electron" \
  --exclude="$ROOT_NAME/third_party/presenton/node_modules" \
  --exclude="$ROOT_NAME/third_party/presenton/servers/fastapi/.venv" \
  --exclude="$ROOT_NAME/third_party/presenton/servers/fastapi/fastembed_cache" \
  --exclude="$ROOT_NAME/third_party/presenton/servers/nextjs/node_modules" \
  --exclude="$ROOT_NAME/third_party/presenton/servers/nextjs/.next-build" \
  --exclude="*/__pycache__" \
  --exclude="*.pyc" \
  --exclude="*.log" \
  -C "$PARENT_DIR" "$ROOT_NAME"

tar -tzf "$OUT_FILE" > "$MANIFEST_FILE"

echo "handoff package: $OUT_FILE"
echo "manifest:        $MANIFEST_FILE"
du -h "$OUT_FILE"

cat <<'EOF'

Package keeps:
- source code for Vibe backend/frontend
- current SQL dump with scene config, preset questions, and saved SQL run history
- docs and handoff instructions
- runtime_data/artifacts and Presenton exports for customer display
- third_party/presenton and deer-flow source without rebuildable dependencies

Package excludes:
- .venv, node_modules, .next/.next-build
- logs, pid files, caches, pycache
- deploy/fz-workflow-demo.app.env because it may contain real API keys

Receiver must create deploy/fz-workflow-demo.app.env from deploy/fz-workflow-demo.app.env.example.
EOF
