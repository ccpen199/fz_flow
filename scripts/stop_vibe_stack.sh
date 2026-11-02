#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PID_DIR="$ROOT_DIR/runtime_data/pids"
BACKEND_PORT="${BACKEND_PORT:-18900}"
FRONTEND_PORT="${FRONTEND_PORT:-18901}"
# 可选服务：PPT 编辑器（TrainPPTAgent / vite dev server）。
# 没在跑时 release_port 只会打印「已空闲」，不会报错。
PPT_EDITOR_PORT="${PPT_EDITOR_PORT:-5173}"

# ── 为什么必须「等到端口真正空闲」才返回 ──────────────────────────────
# uvicorn 收到 SIGTERM 后**不是立刻退出**：它先停止接受新连接，再等在途请求跑完，
# 最后才释放监听 socket。画布这边一个在途请求可能是「把某张图换成折线图」这类要
# 调 DeepFlow agent 的活儿，实测能悬住 25s 以上，期间端口一直处于 LISTEN。
#
# 本脚本原来的写法是 kill 完立刻 return，**完全不等待**。紧接着跑的 start 脚本
# 看到「端口还在监听」，只再等 2.5s 就 `failed to release port 18900` 退出——
# 用户看到的「其他项目占用端口了？」其实来自这里。
#
# 所以：SIGTERM 后等够 GRACE_S 秒；仍不释放 = 有在途请求悬着，直接 SIGKILL。
# 实测对照：SIGTERM（有在途请求）> 25s 未释放；SIGKILL 0.05s 释放。
GRACE_S="${FZ_STOP_GRACE_S:-10}"
KILL_WAIT_S="${FZ_STOP_KILL_WAIT_S:-5}"

listeners() {
  lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true
}

wait_port_free() {
  local port="$1"
  local i=0
  while [ "$i" -lt $(( GRACE_S * 10 )) ]; do
    if [ -z "$(listeners "$port")" ]; then
      return 0
    fi
    sleep 0.1
    i=$((i + 1))
  done
  return 1
}

stop_pid() {
  local pid_file="$1"
  if [ ! -f "$pid_file" ]; then
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  rm -f "$pid_file"

  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi

  kill "$pid" 2>/dev/null || true
  local i=0
  while [ "$i" -lt $(( GRACE_S * 10 )) ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
    i=$((i + 1))
  done

  echo "  pid $pid 未在 ${GRACE_S}s 内退出（多半有在途请求悬着）→ SIGKILL"
  kill -9 "$pid" 2>/dev/null || true
  return 0
}

# 本脚本的唯一退出条件：端口**真正空闲**才算成功。
release_port() {
  local port="$1"
  local pids
  pids="$(listeners "$port")"

  if [ -z "$pids" ]; then
    echo "  port $port 已空闲"
    return 0
  fi

  local pretty
  pretty="$(echo "$pids" | tr '\n' ' ')"
  echo "  port $port <- pid(s) $pretty → SIGTERM，最多等 ${GRACE_S}s"
  echo "$pids" | xargs kill >/dev/null 2>&1 || true

  if wait_port_free "$port"; then
    echo "  port $port 已释放（SIGTERM）"
    return 0
  fi

  echo "  ${GRACE_S}s 内未释放（在途请求悬着）→ SIGKILL"
  echo "$pids" | xargs kill -9 >/dev/null 2>&1 || true

  local i=0
  while [ "$i" -lt $(( KILL_WAIT_S * 10 )) ]; do
    if [ -z "$(listeners "$port")" ]; then
      echo "  port $port 已释放（SIGKILL）"
      return 0
    fi
    sleep 0.1
    i=$((i + 1))
  done

  echo "  failed to release port $port（残留 pid: $(listeners "$port")）" >&2
  return 1
}

echo "stopping vibe stack"
stop_pid "$PID_DIR/vibe_backend.pid"
stop_pid "$PID_DIR/vibe_frontend.pid"
stop_pid "$PID_DIR/ppt_editor.pid"

RC=0
if ! release_port "$BACKEND_PORT"; then
  RC=1
fi
if ! release_port "$FRONTEND_PORT"; then
  RC=1
fi
# 编辑器不在跑是常态（可选项），所以这里失败也只提醒，不把它算进退出码。
if ! release_port "$PPT_EDITOR_PORT"; then
  echo "  注意：PPT 编辑器端口 $PPT_EDITOR_PORT 仍被占用（不影响后端/前端）" >&2
fi

if [ "$RC" -ne 0 ]; then
  echo "stopped vibe stack (with errors — 端口仍被占用)" >&2
  exit 1
fi
echo "stopped vibe stack（端口已确认空闲）"
