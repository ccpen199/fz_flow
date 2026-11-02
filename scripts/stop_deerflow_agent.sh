#!/usr/bin/env bash
# 停止 DeepFlow 多轮对话 agent（deer-flow 的 LangGraph 服务，默认 2124）。
#
# ⚠️ 关键：**必须等到端口真正释放再返回**。
# LangGraph 收到 SIGTERM 后要 flush 持久化、停 worker，才释放监听端口；
# 若本脚本立刻返回，紧随其后的 start_deerflow_agent.sh 会看到"端口还在监听"
# 而跳过启动 —— 结果是 stop→start 之后 agent 反而是死的（曾真实发生）。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/runtime_data/pids/deerflow_agent.pid"
PORT="${DEEPFLOW_AGENT_PORT:-2124}"

port_listeners() {
  lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true
}

stopped=0
if [ -f "$PID_FILE" ]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    stopped=1
  fi
  rm -f "$PID_FILE"
fi

for pid in $(port_listeners); do
  kill "$pid" 2>/dev/null || true
  stopped=1
done

# 等端口释放：先温柔等 10 秒，仍不退就 SIGKILL，再等 10 秒。
i=0
while [ "$i" -lt 40 ]; do
  if [ -z "$(port_listeners)" ]; then
    break
  fi
  if [ "$i" -eq 20 ]; then
    for pid in $(port_listeners); do
      kill -9 "$pid" 2>/dev/null || true
    done
  fi
  sleep 0.5
  i=$((i + 1))
done

if [ -n "$(port_listeners)" ]; then
  echo "[deerflow-agent] 停止失败：端口 ${PORT} 仍被占用" >&2
  exit 1
fi

if [ "$stopped" -eq 1 ]; then
  echo "[deerflow-agent] 已停止（端口 ${PORT} 已释放）"
else
  echo "[deerflow-agent] 未发现运行中的实例（端口 ${PORT} 空闲）"
fi
