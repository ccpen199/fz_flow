#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-18900}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-18901}"
# PPT 编辑器（TrainPPTAgent 内核 PPTist）——对话画布右侧那一栏的 iframe。
# 它是独立的 vite dev server；不跑起来时「生成 PPT」仍能出卡、能导出 .pptx，
# 只是最右侧的编辑器打不开（iframe 会显示连接失败）。
PPT_EDITOR_PORT="${PPT_EDITOR_PORT:-5173}"
PPT_EDITOR_DIR="${TRAIN_PPT_AGENT_DIR:-/Users/chen/Desktop/Cursor_project/parttime-job-agnet/ppt-agent/otherPro/TrainPPTAgent/frontend}"
LOG_DIR="$ROOT_DIR/runtime_data/logs"
PID_DIR="$ROOT_DIR/runtime_data/pids"

# ── 两个时间常量，都是为了同一个坑 ────────────────────────────────────
# 坑：uvicorn 收到 SIGTERM 会「先停 accept、再等在途请求跑完」，有在途请求时
#     实测 25s+ 都不释放端口。原来的 free_port 只等 2.5s 就报
#     `failed to release port 18900` 退出——这就是用户看到的「服务起不来」。
#     不是别的程序占端口，是**自己上一次的实例还没关完**。
# 另一个坑：原来判断「已在运行」只看端口在不在监听。端口被一个**半死实例**
#     （accept 已停、socket 还占着）占着时会直接跳过启动，服务看起来"起过"了，
#     实际 /health 根本不通。
GRACE_S="${FZ_STOP_GRACE_S:-10}"
KILL_WAIT_S="${FZ_STOP_KILL_WAIT_S:-5}"
START_WAIT_S="${FZ_START_WAIT_S:-15}"
HEALTH_TIMEOUT_S="${FZ_HEALTH_TIMEOUT_S:-2}"

mkdir -p "$LOG_DIR" "$PID_DIR"

listeners() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -Fp 2>/dev/null | sed -n 's/^p//p' || true
}

# 真的能应答才算活着。/health 通 = 应用在跑；只判端口会把半死实例当活人。
service_healthy() {
  local name="$1"
  local port="$2"
  local probe
  case "$name" in
    vibe_backend)  probe="http://127.0.0.1:${port}/health" ;;
    vibe_frontend) probe="http://127.0.0.1:${port}/agent.html" ;;
    ppt_editor)    probe="http://127.0.0.1:${port}/" ;;
    *)             probe="http://127.0.0.1:${port}/" ;;
  esac
  curl -fsS --noproxy '*' --max-time "$HEALTH_TIMEOUT_S" "$probe" >/dev/null 2>&1
}

# 把端口清空才返回。先讲理（SIGTERM 等 GRACE_S 秒），讲不通就动手（SIGKILL）。
free_port() {
  local port="$1"
  local pids
  pids="$(listeners "$port")"

  if [ -z "$pids" ]; then
    return 0
  fi

  local pretty
  pretty="$(echo "$pids" | tr '\n' ' ')"
  echo "  port $port <- pid(s) $pretty → SIGTERM，最多等 ${GRACE_S}s"
  echo "$pids" | xargs kill >/dev/null 2>&1 || true

  local i=0
  while [ "$i" -lt $(( GRACE_S * 10 )) ]; do
    if [ -z "$(listeners "$port")" ]; then
      echo "  port $port 已释放（SIGTERM）"
      return 0
    fi
    sleep 0.1
    i=$((i + 1))
  done

  echo "  ${GRACE_S}s 内未释放（在途请求悬着）→ SIGKILL"
  echo "$pids" | xargs kill -9 >/dev/null 2>&1 || true

  i=0
  while [ "$i" -lt $(( KILL_WAIT_S * 10 )) ]; do
    if [ -z "$(listeners "$port")" ]; then
      echo "  port $port 已释放（SIGKILL）"
      return 0
    fi
    sleep 0.1
    i=$((i + 1))
  done

  echo "failed to release port $port（残留 pid: $(listeners "$port")）" >&2
  return 1
}

start_if_needed() {
  local name="$1"
  local pid_file="$2"
  local port="$3"
  local cmd="$4"
  local log_file="$LOG_DIR/${name}.log"
  local running_pid=""
  local service_pid=""
  local launcher_pid=""
  local i=0

  # 1) 端口在监听 **且健康检查通过** → 真的已在运行，什么都不动
  #    （保住用户正在跑的在途请求，别把好好的服务掐了）。
  if [ -n "$(listeners "$port")" ] && service_healthy "$name" "$port"; then
    running_pid="$(listeners "$port" | head -n 1)"
    echo "$running_pid" >"$pid_file"
    echo "[$name] already running and healthy pid=$running_pid"
    return 0
  fi

  # 2) 端口在监听但健康检查不通 → 半死残留（SIGTERM 后 accept 停了、socket 还占着）。
  #    必须先清掉，否则新进程会 [Errno 48] Address already in use 直接死。
  if [ -n "$(listeners "$port")" ]; then
    echo "[$name] 端口在监听但健康检查失败（半死残留）→ 先清理"
    free_port "$port"
  fi

  rm -f "$pid_file"

  echo "[$name] starting..."
  nohup bash -lc "$cmd" >"$log_file" 2>&1 &
  launcher_pid="$!"

  # 等到「端口绑定 + 健康检查通过」为止；只绑端口不够——那可能只是还没跑起来。
  while [ "$i" -lt $(( START_WAIT_S * 4 )) ]; do
    service_pid="$(listeners "$port" | head -n 1)"
    if [ -n "$service_pid" ] && service_healthy "$name" "$port"; then
      echo "$service_pid" >"$pid_file"
      echo "[$name] running pid=$service_pid"
      return 0
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

start_if_needed "vibe_backend" "$PID_DIR/vibe_backend.pid" "$BACKEND_PORT" "cd '$ROOT_DIR' && HOST='$BACKEND_HOST' PORT='$BACKEND_PORT' bash scripts/start_vibe_backend.sh"
start_if_needed "vibe_frontend" "$PID_DIR/vibe_frontend.pid" "$FRONTEND_PORT" "cd '$ROOT_DIR' && HOST='$FRONTEND_HOST' PORT='$FRONTEND_PORT' bash scripts/start_vibe_frontend.sh"

# ── PPT 编辑器（可选服务）─────────────────────────────────────────────
# 目录/依赖不在就**跳过而不是失败**：它属于可选的展示层，
# 缺了它整个栈仍可用（「导出 .pptx」走的是后端 python-pptx，不受影响）。
if [ -x "$PPT_EDITOR_DIR/node_modules/.bin/vite" ]; then
  # `nohup bash -lc` 会重置 PATH，而 vite 的 shebang 是 `#!/usr/bin/env node`，
  # 所以这里必须解析出 node 的**绝对路径**再传进去，不能指望登录 shell 里找得到。
  NODE_BIN="${NODE_BIN:-$(command -v node 2>/dev/null || true)}"
  if [ -z "$NODE_BIN" ] && [ -x /opt/homebrew/bin/node ]; then
    NODE_BIN="/opt/homebrew/bin/node"
  fi
  if [ -n "$NODE_BIN" ] && [ -x "$NODE_BIN" ]; then
    start_if_needed "ppt_editor" "$PID_DIR/ppt_editor.pid" "$PPT_EDITOR_PORT" \
      "cd '$PPT_EDITOR_DIR' && exec '$NODE_BIN' node_modules/.bin/vite --port '$PPT_EDITOR_PORT' --strictPort --host 127.0.0.1" || true
  else
    echo "[ppt_editor] skipped —— 找不到 node（设 NODE_BIN=/path/to/node 可指定）"
  fi
else
  echo "[ppt_editor] skipped —— 未找到 $PPT_EDITOR_DIR/node_modules/.bin/vite"
  echo "               （编辑器是可选服务；缺它只影响最右侧那一栏，不影响生成与导出）"
fi

echo "Backend:  http://${BACKEND_HOST}:${BACKEND_PORT}"
echo "Frontend: http://${FRONTEND_HOST}:${FRONTEND_PORT}"
echo "PptEditor: http://127.0.0.1:${PPT_EDITOR_PORT}/editor   （可选；画布最右侧那一栏）"
echo "Logs:     $LOG_DIR"
