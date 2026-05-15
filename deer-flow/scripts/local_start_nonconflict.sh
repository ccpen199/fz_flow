#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT_DIR/logs"
RUN_DIR="$ROOT_DIR/.run"
mkdir -p "$LOG_DIR" "$RUN_DIR"

LANGGRAPH_PORT="${LANGGRAPH_PORT:-2124}"
GATEWAY_PORT="${GATEWAY_PORT:-8101}"
FRONTEND_PORT="${FRONTEND_PORT:-3101}"
APP_PORT="${APP_PORT:-2126}"
NGINX_CONF="$RUN_DIR/nginx.local.nonconflict.conf"

check_port() {
  local port="$1"
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: port ${port} is already in use"
    exit 1
  fi
}

for port in "$LANGGRAPH_PORT" "$GATEWAY_PORT" "$FRONTEND_PORT" "$APP_PORT"; do
  check_port "$port"
done

if [[ ! -f "$ROOT_DIR/config.yaml" ]]; then
  python3 "$ROOT_DIR/scripts/configure.py"
fi

python3 "$ROOT_DIR/scripts/prepare_local_codex_config.py"

cat >"$NGINX_CONF" <<EOF
events {
    worker_connections 1024;
}
pid logs/nginx-deerflow.pid;
http {
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    types_hash_max_size 2048;
    access_log logs/nginx-access.log;
    error_log logs/nginx-error.log;

    upstream gateway {
        server 127.0.0.1:${GATEWAY_PORT};
    }

    upstream langgraph {
        server 127.0.0.1:${LANGGRAPH_PORT};
    }

    upstream frontend {
        server 127.0.0.1:${FRONTEND_PORT};
    }

    server {
        listen ${APP_PORT};
        listen [::]:${APP_PORT};
        server_name _;

        proxy_hide_header 'Access-Control-Allow-Origin';
        proxy_hide_header 'Access-Control-Allow-Methods';
        proxy_hide_header 'Access-Control-Allow-Headers';
        proxy_hide_header 'Access-Control-Allow-Credentials';

        add_header 'Access-Control-Allow-Origin' '*' always;
        add_header 'Access-Control-Allow-Methods' 'GET, POST, PUT, DELETE, PATCH, OPTIONS' always;
        add_header 'Access-Control-Allow-Headers' '*' always;

        if (\$request_method = 'OPTIONS') {
            return 204;
        }

        location /api/langgraph/ {
            rewrite ^/api/langgraph/(.*) /\$1 break;
            proxy_pass http://langgraph;
            proxy_http_version 1.1;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_set_header Connection '';
            proxy_buffering off;
            proxy_cache off;
            proxy_set_header X-Accel-Buffering no;
            proxy_connect_timeout 600s;
            proxy_send_timeout 600s;
            proxy_read_timeout 600s;
            chunked_transfer_encoding on;
        }

        location /api/models { proxy_pass http://gateway; proxy_http_version 1.1; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto \$scheme; }
        location /api/memory { proxy_pass http://gateway; proxy_http_version 1.1; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto \$scheme; }
        location /api/mcp { proxy_pass http://gateway; proxy_http_version 1.1; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto \$scheme; }
        location /api/skills { proxy_pass http://gateway; proxy_http_version 1.1; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto \$scheme; }
        location /api/agents { proxy_pass http://gateway; proxy_http_version 1.1; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto \$scheme; }
        location ~ ^/api/threads/[^/]+/uploads {
            proxy_pass http://gateway;
            proxy_http_version 1.1;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
            client_max_body_size 100M;
            proxy_request_buffering off;
        }
        location ~ ^/api/threads {
            proxy_pass http://gateway;
            proxy_http_version 1.1;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
        }
        location /docs { proxy_pass http://gateway; proxy_http_version 1.1; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto \$scheme; }
        location /redoc { proxy_pass http://gateway; proxy_http_version 1.1; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto \$scheme; }
        location /openapi.json { proxy_pass http://gateway; proxy_http_version 1.1; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto \$scheme; }
        location /health { proxy_pass http://gateway; proxy_http_version 1.1; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto \$scheme; }

        location / {
            proxy_pass http://frontend;
            proxy_http_version 1.1;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection 'upgrade';
            proxy_cache_bypass \$http_upgrade;
            proxy_connect_timeout 600s;
            proxy_send_timeout 600s;
            proxy_read_timeout 600s;
        }
    }
}
EOF

echo "Starting LangGraph on ${LANGGRAPH_PORT}..."
nohup sh -c "cd \"$ROOT_DIR/backend\" && exec env NO_COLOR=1 uv run langgraph dev --port \"$LANGGRAPH_PORT\" --no-browser --allow-blocking --no-reload >\"$LOG_DIR/langgraph.log\" 2>&1" >/dev/null 2>&1 &
echo $! >"$RUN_DIR/langgraph.pid"

"$ROOT_DIR/scripts/wait-for-port.sh" "$LANGGRAPH_PORT" 60 "LangGraph"

echo "Starting Gateway on ${GATEWAY_PORT}..."
nohup sh -c "cd \"$ROOT_DIR/backend\" && exec env PYTHONPATH=. uv run uvicorn app.gateway.app:app --host 127.0.0.1 --port \"$GATEWAY_PORT\" >\"$LOG_DIR/gateway.log\" 2>&1" >/dev/null 2>&1 &
echo $! >"$RUN_DIR/gateway.pid"

"$ROOT_DIR/scripts/wait-for-port.sh" "$GATEWAY_PORT" 30 "Gateway"

echo "Starting Frontend on ${FRONTEND_PORT}..."
nohup sh -c "cd \"$ROOT_DIR/frontend\" && exec env PORT=\"$FRONTEND_PORT\" COREPACK_ENABLE_AUTO_PIN=0 corepack pnpm run dev >\"$LOG_DIR/frontend.log\" 2>&1" >/dev/null 2>&1 &
echo $! >"$RUN_DIR/frontend.pid"

"$ROOT_DIR/scripts/wait-for-port.sh" "$FRONTEND_PORT" 120 "Frontend"

echo "Starting Nginx on ${APP_PORT}..."
nohup sh -c "cd \"$ROOT_DIR\" && exec nginx -g 'daemon off;' -c \"$NGINX_CONF\" -p \"$ROOT_DIR\" >\"$LOG_DIR/nginx.log\" 2>&1" >/dev/null 2>&1 &
echo $! >"$RUN_DIR/nginx.pid"

"$ROOT_DIR/scripts/wait-for-port.sh" "$APP_PORT" 15 "Nginx"

echo "OK"
echo "App:      http://127.0.0.1:${APP_PORT}"
echo "Health:   http://127.0.0.1:${APP_PORT}/health"
echo "Gateway:  http://127.0.0.1:${GATEWAY_PORT}"
echo "Frontend: http://127.0.0.1:${FRONTEND_PORT}"
