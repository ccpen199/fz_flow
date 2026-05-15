from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config_store import (
    add_field,
    add_relation,
    create_scene,
    ensure_preset_scenes,
    get_scene,
    list_scenes,
)
from .engine import export_ppt, run_analysis
from .mock_data import ensure_demo_database

app = FastAPI(title="Vibe Data Analysis Demo", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/ui", StaticFiles(directory=str(STATIC_DIR), html=True), name="ui")

SESSIONS: Dict[str, Dict[str, Any]] = {}


class GoalRequest(BaseModel):
    goal: str


class SceneCreateRequest(BaseModel):
    name: str
    description: str = ""


class FieldCreateRequest(BaseModel):
    table_name: str
    field_name: str
    semantic_name: str
    description: str = ""
    role: str = "dimension"
    enabled: bool = True


class RelationCreateRequest(BaseModel):
    left_table: str
    left_field: str
    right_table: str
    right_field: str
    join_type: str = "INNER"
    note: str = ""


@app.on_event("startup")
def _on_startup() -> None:
    ensure_demo_database()
    ensure_preset_scenes()


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config/relation-suggestions")
def relation_suggestions() -> Dict[str, Any]:
    return {
        "suggestions": [
            {
                "left_table": "product_snapshot",
                "left_field": "brand_id",
                "right_table": "dim_brand",
                "right_field": "brand_id",
                "join_type": "INNER",
                "confidence": 0.98,
                "reason": "字段名一致且维表主键匹配",
            },
            {
                "left_table": "product_snapshot",
                "left_field": "platform_id",
                "right_table": "dim_platform",
                "right_field": "platform_id",
                "join_type": "INNER",
                "confidence": 0.98,
                "reason": "字段名一致且维表主键匹配",
            },
            {
                "left_table": "product_snapshot",
                "left_field": "category_id",
                "right_table": "dim_category",
                "right_field": "category_id",
                "join_type": "INNER",
                "confidence": 0.98,
                "reason": "字段名一致且维表主键匹配",
            },
        ]
    }


@app.get("/api/config/scenes")
def config_list_scenes() -> Dict[str, Any]:
    return {"scenes": list_scenes()}


@app.post("/api/config/scenes/preset-sync")
def config_sync_presets() -> Dict[str, Any]:
    scenes = ensure_preset_scenes()
    return {"scenes": scenes, "count": len(scenes)}


@app.post("/api/config/scenes")
def config_create_scene(req: SceneCreateRequest) -> Dict[str, Any]:
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="scene name is required")
    return create_scene(req.name, req.description)


@app.get("/api/config/scenes/{scene_id}")
def config_get_scene(scene_id: str) -> Dict[str, Any]:
    scene = get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found.")
    return scene


@app.post("/api/config/scenes/{scene_id}/fields")
def config_add_field(scene_id: str, req: FieldCreateRequest) -> Dict[str, Any]:
    try:
        return add_field(
            scene_id=scene_id,
            table_name=req.table_name,
            field_name=req.field_name,
            semantic_name=req.semantic_name,
            description=req.description,
            role=req.role,
            enabled=req.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/config/scenes/{scene_id}/relations")
def config_add_relation(scene_id: str, req: RelationCreateRequest) -> Dict[str, Any]:
    try:
        return add_relation(
            scene_id=scene_id,
            left_table=req.left_table,
            left_field=req.left_field,
            right_table=req.right_table,
            right_field=req.right_field,
            join_type=req.join_type,
            note=req.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/sessions")
def create_session() -> Dict[str, str]:
    session_id = uuid4().hex[:12]
    SESSIONS[session_id] = {
        "session_id": session_id,
        "goals": [],
        "results": [],
        "slides": [],
    }
    return {"session_id": session_id}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    return session


@app.post("/api/sessions/{session_id}/analyze")
def analyze(session_id: str, req: GoalRequest) -> Dict[str, Any]:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    if not req.goal.strip():
        raise HTTPException(status_code=400, detail="Goal is empty.")

    result = run_analysis(req.goal.strip())
    result_dict = {
        "goal": result.goal,
        "query_plan": result.query_plan,
        "sql": result.sql,
        "columns": result.columns,
        "rows": result.rows,
        "insight": result.insight,
        "recommendation": result.recommendation,
        "slide": result.slide,
    }
    session["goals"].append(req.goal.strip())
    session["results"].append(result_dict)
    session["slides"].append(result.slide)
    return result_dict


@app.post("/api/sessions/{session_id}/export")
def export(session_id: str) -> Dict[str, str]:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    slides: List[Dict[str, str]] = session["slides"]
    if not slides:
        raise HTTPException(status_code=400, detail="No slides to export.")
    file_path = export_ppt(session_id, slides)
    return {"file_path": file_path, "download_url": f"/api/download/{session_id}"}


@app.get("/api/download/{session_id}")
def download(session_id: str) -> FileResponse:
    root = Path(__file__).resolve().parents[2]
    file_path = root / "output" / f"analysis_{session_id}.pptx"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="PPT not found.")
    return FileResponse(
        path=str(file_path),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=file_path.name,
    )
