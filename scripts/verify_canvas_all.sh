#!/usr/bin/env bash
# 新画布（agent.html）改动后的一键自查。全部只读，不启动服务、不写业务数据。
#
#   bash scripts/verify_canvas_all.sh
#
# 覆盖六件事：
#   1. 后端单元测试
#   2. 零假数据守卫（静态 + 运行时回溯真实 MySQL）
#   3. 图表类型映射守卫（防"图静默变白"）
#   4. percent 刻轴口径守卫
#   5. 卡片/配置校准守卫（按需配置必须接上卡片侧，且不许指标兜底）
#   6. 前端 JS 语法
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

NODE_BIN="$(command -v node || true)"

failed=0
run() {
  local label="$1"
  shift
  echo "── ${label}"
  if "$@"; then
    echo "   ✅ 通过"
  else
    echo "   ❌ 未通过"
    failed=$((failed + 1))
  fi
  echo
}

run "后端单元测试" \
  env -u PYTHONPATH "$PYTHON_BIN" -m pytest apps/vibe_backend/tests/ -q

run "零假数据守卫" \
  env -u PYTHONPATH "$PYTHON_BIN" scripts/verify_canvas_no_fake_data.py

run "图表类型映射守卫" \
  env -u PYTHONPATH "$PYTHON_BIN" scripts/verify_chart_type_mapping.py

run "percent 刻轴口径守卫" \
  env -u PYTHONPATH "$PYTHON_BIN" scripts/verify_percent_axis.py

run "卡片/配置校准守卫" \
  env -u PYTHONPATH "$PYTHON_BIN" scripts/verify_card_config_alignment.py

if [ -n "$NODE_BIN" ]; then
  run "前端 JS 语法" "$NODE_BIN" --check apps/vibe_frontend/static/agent.js
else
  echo "── 前端 JS 语法"
  echo "   ⚠️ 跳过：PATH 里没有 node"
  echo
fi

if [ "$failed" -eq 0 ]; then
  echo "=== 结论：✅ 全部通过 ==="
else
  echo "=== 结论：❌ ${failed} 项未通过 ==="
fi
exit "$failed"
