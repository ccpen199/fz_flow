#!/usr/bin/env bash
# DeepFlow（deer-flow）多轮对话 agent —— 只启动新链路需要的那一个服务。
#
# 用途：fz_workflow 的新画布（agent.html）多轮对话由 deer-flow 的 LangGraph
#       agent `fz-canvas` 做意图与槽位判定；本脚本把该服务起在 2124 端口。
# 说明：只起 LangGraph（聊天/推理入口），不起 gateway / frontend / nginx——
#       画布链路不需要它们。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEER_DIR="${DEERFLOW_DIR:-$ROOT_DIR/deer-flow}"
LOG_DIR="$ROOT_DIR/runtime_data/logs"
PID_FILE="$ROOT_DIR/runtime_data/pids/deerflow_agent.pid"
PORT="${DEEPFLOW_AGENT_PORT:-2124}"
VENV_PY="$DEER_DIR/backend/.venv/bin/langgraph"
LOG_FILE="$LOG_DIR/deerflow_agent.log"

mkdir -p "$LOG_DIR" "$(dirname "$PID_FILE")"

if [ ! -x "$VENV_PY" ]; then
  echo "找不到 deer-flow 的 venv：$VENV_PY" >&2
  echo "请先在 $DEER_DIR/backend 下安装依赖（uv sync / pip install -e .）" >&2
  exit 1
fi

agent_ok() {
  curl -fsS --noproxy '*' --max-time 2 "http://127.0.0.1:${PORT}/ok" >/dev/null 2>&1
}

port_busy() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
}

# 已经在跑（健康检查真的通）才跳过。
# ⚠️ 不能只看"端口被监听"：端口可能被上一次还没退干净的实例占着，
# 这时若跳过启动，就会出现"stop 之后 agent 反而是死的"（曾真实发生）。
if agent_ok; then
  echo "[deerflow-agent] 已在运行（http://127.0.0.1:${PORT}），跳过启动"
  exit 0
fi

if port_busy; then
  # 端口被占但健康检查不通：先给它一点时间启动（可能刚被别的调用拉起），
  # 仍是死的就判定为残留实例，清掉再起。
  echo "[deerflow-agent] 端口 ${PORT} 被占用但健康检查不通，等待其就绪…"
  for _ in $(seq 1 10); do
    if agent_ok; then
      echo "[deerflow-agent] 已在运行（http://127.0.0.1:${PORT}），跳过启动"
      exit 0
    fi
    sleep 1
  done
  echo "[deerflow-agent] 判定为残留监听，清理后重新启动"
  for pid in $(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true); do
    kill "$pid" 2>/dev/null || true
  done
  for _ in $(seq 1 20); do
    port_busy || break
    for pid in $(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true); do
      kill -9 "$pid" 2>/dev/null || true
    done
    sleep 0.5
  done
  if port_busy; then
    echo "[deerflow-agent] 端口 ${PORT} 无法释放，放弃启动" >&2
    exit 1
  fi
fi

# 内网网关密钥从 deer-flow/.env 读取，注入子进程（不打印）
GATEWAY_KEY=""
if [ -f "$DEER_DIR/.env" ]; then
  GATEWAY_KEY="$(grep -E '^FZ_GATEWAY_API_KEY=' "$DEER_DIR/.env" | head -1 | cut -d= -f2- || true)"
fi

# DeepSeek 官方 API 密钥（config.yaml 的 fz-deepseek 用到）同样从 .env 注入，
# 否则切到 deepseek 档时 agent 会因为 $DEEPSEEK_API_KEY 解析不到而报错。
DEEPSEEK_KEY=""
if [ -f "$DEER_DIR/.env" ]; then
  DEEPSEEK_KEY="$(grep -E '^DEEPSEEK_API_KEY=' "$DEER_DIR/.env" | head -1 | cut -d= -f2- || true)"
fi

echo "[deerflow-agent] 启动中（端口 ${PORT}）…"
cd "$DEER_DIR/backend"
# ⚠️ 必须显式摘掉 HTTP_PROXY/HTTPS_PROXY：运行环境（含沙箱）常注入代理变量，
# httpx/openai 会因此把请求行写成「绝对 URL 形式」
# （POST http://20.255.59.22:4000/v1/chat/completions 而不是 POST /v1/chat/completions），
# 内网网关对畸形请求行直接回 404。症状：同一会话第一轮正常、第二轮起必失败。
# 本 agent 只访问内网网关与本机，永远不需要代理。
nohup env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  FZ_GATEWAY_API_KEY="$GATEWAY_KEY" DEEPSEEK_API_KEY="$DEEPSEEK_KEY" \
  PYTHONPATH=. NO_COLOR=1 NO_PROXY='*' no_proxy='*' \
  "$VENV_PY" dev --port "$PORT" --no-browser --allow-blocking --no-reload \
  >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"

for _ in $(seq 1 90); do
  if agent_ok; then
    real_pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
    if [ -n "$real_pid" ]; then
      echo "$real_pid" >"$PID_FILE"
    fi
    echo "[deerflow-agent] 就绪：http://127.0.0.1:$PORT （pid=${real_pid:-unknown}）"
    echo "[deerflow-agent] 日志：$LOG_FILE"
    exit 0
  fi
  sleep 1
done

# 启动失败要把半死的进程收掉，否则留下的监听端口会让下一次启动误判"已在运行"。
echo "[deerflow-agent] 启动超时，清理未就绪的进程" >&2
for pid in $(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true); do
  kill -9 "$pid" 2>/dev/null || true
done
rm -f "$PID_FILE"
echo "[deerflow-agent] 最近日志：" >&2
tail -n 30 "$LOG_FILE" >&2 || true
exit 1
