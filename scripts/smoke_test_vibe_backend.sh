#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR:${PYTHONPATH:-}"

PY_BIN="$(command -v python3)"
if [ -x "$ROOT_DIR/deer-flow/backend/.venv/bin/python" ]; then
  PY_BIN="$ROOT_DIR/deer-flow/backend/.venv/bin/python"
elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PY_BIN="$ROOT_DIR/.venv/bin/python"
fi

"$PY_BIN" - <<'PY'
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient

from apps.vibe_backend.app.main import app
import apps.vibe_backend.app.store as store

client = TestClient(app)

scene = client.post("/api/v1/scenes", json={"name": "冒烟测试场景", "description": "smoke"}).json()
scene_id = scene["scene_id"]

client.post(
    f"/api/v1/scenes/{scene_id}/fields",
    json={
        "table_name": "product_snapshot",
        "field_name": "sale_price",
        "semantic_name": "销售价",
        "description": "",
        "role": "metric",
        "enabled": True,
    },
)
client.post(
    f"/api/v1/scenes/{scene_id}/fields",
    json={
        "table_name": "dim_brand",
        "field_name": "brand_name",
        "semantic_name": "品牌",
        "description": "",
        "role": "dimension",
        "enabled": True,
    },
)
client.post(
    f"/api/v1/scenes/{scene_id}/fields",
    json={
        "table_name": "dim_platform",
        "field_name": "platform_name",
        "semantic_name": "平台",
        "description": "",
        "role": "dimension",
        "enabled": True,
    },
)
client.post(
    f"/api/v1/scenes/{scene_id}/relations",
    json={
        "left_table": "product_snapshot",
        "left_field": "brand_id",
        "right_table": "dim_brand",
        "right_field": "brand_id",
        "join_type": "INNER",
        "note": "",
    },
)
client.post(
    f"/api/v1/scenes/{scene_id}/relations",
    json={
        "left_table": "product_snapshot",
        "left_field": "platform_id",
        "right_table": "dim_platform",
        "right_field": "platform_id",
        "join_type": "INNER",
        "note": "",
    },
)

session = client.post(
    "/api/v1/analysis/sessions",
    json={"scene_id": scene_id, "global_goal": "分析品牌销售并输出首轮汇报"},
).json()
session_id = session["session_id"]
thread_context = client.get(f"/api/v1/analysis/sessions/{session_id}/thread-context").json()
chat_turn = client.post(
    f"/api/v1/analysis/sessions/{session_id}/deerflow/chat",
    json={"message": "请回复一句测试"},
).json()
chat_turns = client.get(f"/api/v1/analysis/sessions/{session_id}/deerflow/chat-turns").json()

client.post(f"/api/v1/analysis/sessions/{session_id}/plan")
query_run = client.post(f"/api/v1/analysis/sessions/{session_id}/current-query/execute").json()
slide = client.get(f"/api/v1/analysis/sessions/{session_id}/current-slide").json()
slide2 = client.post(
    f"/api/v1/analysis/sessions/{session_id}/current-slide/regenerate",
    json={"style_hint": "简洁商务风", "structure_hint": "对比页"},
).json()
deck = client.post(f"/api/v1/analysis/sessions/{session_id}/current-slide/approve").json()
artifact = client.post(f"/api/v1/decks/{deck['deck_id']}/export").json()
download = client.get(artifact["download_url"])
bridge_health = client.get("/api/v1/bridge/deerflow/health").json()
bridge_skills = client.get("/api/v1/bridge/deerflow/skills").json()

assert query_run["rows_count"] > 0
assert query_run["lineage"]["execution_mode"] == "mysql"
assert len(query_run["safety_checks"]) > 0
assert all(check["passed"] for check in query_run["safety_checks"] if check["type"] != "row_limit")
assert len(slide["findings"]) > 0
assert slide["page_type"] in {"overview", "comparison", "trend", "risk", "summary", "root_cause"}
assert "chart_type" in slide["chart_spec"]
assert slide2["version"] >= 2
assert artifact["artifact_id"]
assert download.status_code == 200
artifact_path = Path(artifact["local_path"])
assert artifact_path.exists()
with ZipFile(artifact_path, "r") as archive:
    names = set(archive.namelist())
assert "ppt/presentation.xml" in names
assert "ppt/slides/slide1.xml" in names
assert session["deerflow_thread_id"]
assert thread_context["workspace_ready"] is True
assert chat_turn["deerflow_thread_id"] == session["deerflow_thread_id"]
assert len(chat_turns) >= 1
assert bridge_health["service"] == "deerflow-bridge"
assert "mode" in bridge_skills

store.SCENES.clear()
store.SESSIONS.clear()
store.QUERY_PLANS.clear()
store.QUERY_RUNS.clear()
store.SLIDE_DRAFTS.clear()
store.DECKS.clear()
store.ARTIFACTS.clear()
store.DEERFLOW_CHAT_TURNS.clear()
store.load_state()

assert len(store.SCENES) >= 1
assert len(store.SESSIONS) >= 1
assert len(store.DECKS) >= 1
assert any(session.deerflow_thread_id for session in store.SESSIONS.values())
assert any(turns for turns in store.DEERFLOW_CHAT_TURNS.values())

print("SMOKE_OK", deck["deck_id"], artifact["artifact_id"])

bad_scene = client.post("/api/v1/scenes", json={"name": "非法查询场景", "description": "bad"}).json()
bad_scene_id = bad_scene["scene_id"]
client.post(
    f"/api/v1/scenes/{bad_scene_id}/relations",
    json={
        "left_table": "product_snapshot",
        "left_field": "brand_id",
        "right_table": "dim_brand",
        "right_field": "not_exist_field",
        "join_type": "INNER",
        "note": "",
    },
)
client.post(
    f"/api/v1/scenes/{bad_scene_id}/fields",
    json={
        "table_name": "product_snapshot",
        "field_name": "sale_price",
        "semantic_name": "销售价",
        "description": "",
        "role": "metric",
        "enabled": True,
    },
)
client.post(
    f"/api/v1/scenes/{bad_scene_id}/fields",
    json={
        "table_name": "dim_brand",
        "field_name": "brand_name",
        "semantic_name": "品牌",
        "description": "",
        "role": "dimension",
        "enabled": True,
    },
)
bad_session = client.post(
    "/api/v1/analysis/sessions",
    json={"scene_id": bad_scene_id, "global_goal": "验证 SQL 安全拦截"},
).json()
client.post(f"/api/v1/analysis/sessions/{bad_session['session_id']}/plan")
bad_query = client.post(f"/api/v1/analysis/sessions/{bad_session['session_id']}/current-query/execute").json()
assert bad_query["status"] == "failed"
assert bad_query["lineage"]["execution_mode"] == "mysql_blocked"
PY
