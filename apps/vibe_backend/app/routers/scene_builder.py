from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.llm_agent_service import LlmAgentService
from ..services.runtime_persistence_service import runtime_persistence_service
from ..services.scene_cache_service import scene_cache_service
from ..services.semantic_field_cache_service import semantic_field_cache_service
from ..store import persist_state

router = APIRouter(prefix="/api/v1/scene-builder", tags=["scene-builder"])
service = LlmAgentService()


class GenerateCandidatesRequest(BaseModel):
    goal: str = ""
    max_tables: int = Field(default=4, ge=1, le=20)
    max_fields_per_table: int = Field(default=12, ge=1, le=50)


class SelectedFieldInput(BaseModel):
    table_name: str
    field_name: str
    semantic_name: str
    role: str = Field(default="dimension", description="metric|dimension|time|filter")
    description: str = ""
    required: bool = False
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    field_type: str = ""
    enabled: bool = True


class SelectedRelationInput(BaseModel):
    left_table: str
    left_field: str
    right_table: str
    right_field: str
    join_type: str = "LEFT"
    cardinality: str = "1:N"
    required: bool = False
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    note: str = ""


class ImportSelectionRequest(BaseModel):
    recommendation_id: str | None = None
    merge_mode: str = Field(default="append", description="append|replace")
    selected_fields: list[SelectedFieldInput] = Field(default_factory=list)
    selected_relations: list[SelectedRelationInput] = Field(default_factory=list)


class DraftStateRequest(BaseModel):
    recommendation: dict


@router.get("/scenes/{scene_id}/draft")
async def get_scene_builder_draft(scene_id: str) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")
    draft = runtime_persistence_service.get_recommendation(scene_id)
    return {
        "ok": True,
        "scene_id": scene_id,
        "draft": draft,
    }


@router.put("/scenes/{scene_id}/draft")
async def save_scene_builder_draft(scene_id: str, body: DraftStateRequest) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")
    if not isinstance(body.recommendation, dict):
        raise HTTPException(status_code=422, detail="recommendation must be object")
    return runtime_persistence_service.save_recommendation(scene_id, body.recommendation)


@router.get("/scenes/{scene_id}/source-schema")
async def get_source_schema(scene_id: str) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")
    snapshot = service.schema_snapshot()
    snapshot["scene_id"] = scene_id
    return snapshot


@router.post("/scenes/{scene_id}/candidates")
async def generate_candidates(scene_id: str, body: GenerateCandidatesRequest) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    rec = service.recommend(
        scene=scene,
        goal=body.goal.strip(),
        max_tables=body.max_tables,
        max_fields_per_table=body.max_fields_per_table,
    )
    candidates = rec.get("candidates", {})
    payload = {
        "ok": True,
        "scene_id": scene_id,
        "scene_version": scene.version,
        "recommendation_id": rec.get("recommendation_id"),
        "goal": rec.get("goal", ""),
        "field_candidates": candidates.get("fields", []),
        "relation_candidates": candidates.get("relations", []),
        "notes": rec.get("notes", []),
        "meta": {
            "provider": rec.get("provider", "heuristic"),
            "mode": rec.get("mode", "local"),
            "table_candidates": candidates.get("tables", []),
            "field_type_list": rec.get("field_type_list", []),
        },
    }
    recommendation = {
        "recommendation_id": payload["recommendation_id"],
        "scene_id": payload["scene_id"],
        "scene_version": payload["scene_version"],
        "provider": payload["meta"]["provider"],
        "mode": payload["meta"]["mode"],
        "goal": payload["goal"],
        "notes": payload["notes"],
        "field_type_list": payload["meta"]["field_type_list"],
        "candidates": {
            "tables": payload["meta"]["table_candidates"],
            "fields": payload["field_candidates"],
            "relations": payload["relation_candidates"],
            "metric_templates": [],
            "regression_questions": [],
        },
    }
    runtime_persistence_service.save_recommendation(scene_id, recommendation)
    return payload


@router.post("/scenes/{scene_id}/imports")
async def import_selected_candidates(scene_id: str, body: ImportSelectionRequest) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    merge_mode = body.merge_mode.strip().lower()
    if merge_mode not in {"append", "replace"}:
        raise HTTPException(status_code=422, detail="merge_mode must be append or replace")

    selected_fields = [
        {
            "candidate_id": f"fld_{uuid4().hex[:12]}",
            "table_name": item.table_name.strip(),
            "field_name": item.field_name.strip(),
            "semantic_name": item.semantic_name.strip(),
            "description": item.description.strip(),
            "role": item.role.strip().lower(),
            "field_type": item.field_type.strip().lower(),
            "required": bool(item.required),
            "selected": True,
            "enabled": bool(item.enabled),
            "confidence": float(item.confidence),
            "reason": "manual_selected",
        }
        for item in body.selected_fields
    ]

    selected_relations = [
        {
            "candidate_id": f"rel_{uuid4().hex[:12]}",
            "left_table": item.left_table.strip(),
            "left_field": item.left_field.strip(),
            "right_table": item.right_table.strip(),
            "right_field": item.right_field.strip(),
            "join_type": item.join_type.strip().upper(),
            "cardinality": item.cardinality.strip().upper(),
            "required": bool(item.required),
            "selected": True,
            "confidence": float(item.confidence),
            "reason": "manual_selected",
            "note": item.note.strip(),
        }
        for item in body.selected_relations
    ]

    candidate_tables = sorted(
        {
            *(item["table_name"] for item in selected_fields if item.get("table_name")),
            *(item["left_table"] for item in selected_relations if item.get("left_table")),
            *(item["right_table"] for item in selected_relations if item.get("right_table")),
        }
    )

    recommendation = {
        "recommendation_id": body.recommendation_id or f"rec_{uuid4().hex[:12]}",
        "scene_id": scene_id,
        "scene_version": scene.version,
        "provider": "manual_review",
        "mode": "no_playbook_import",
        "goal": "",
        "notes": ["import selected candidates without playbook"],
        "candidates": {
            "tables": candidate_tables,
            "fields": selected_fields,
            "relations": selected_relations,
            "metric_templates": [],
            "regression_questions": [],
        },
    }

    validation = service.validate_recommendation(scene=scene, recommendation=recommendation)
    if not validation.get("ok", False):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "selected candidates validation failed",
                "issues": validation.get("issues", []),
            },
        )

    canonical = validation.get("canonical_recommendation", {})
    apply_result = service.apply_to_scene(scene=scene, recommendation=canonical, merge_mode=merge_mode)
    semantic_sync_result = semantic_field_cache_service.upsert_effective_fields_from_candidates(
        scene_id=scene_id,
        selected_fields=selected_fields,
        replace_existing_effective=(merge_mode == "replace"),
    )
    canonical["last_apply_result"] = {
        "apply_result": apply_result,
        "semantic_cache_sync_result": semantic_sync_result,
    }
    runtime_persistence_service.save_recommendation(scene_id, canonical)
    scene_cache_service.upsert_scene(scene)
    persist_state()

    return {
        "ok": True,
        "scene_id": scene_id,
        "recommendation_id": canonical.get("recommendation_id"),
        "apply_result": apply_result,
        "semantic_cache_sync_result": semantic_sync_result,
        "validate_result": {
            "ok": True,
            "error_count": validation.get("error_count", 0),
            "warning_count": validation.get("warning_count", 0),
            "issues": validation.get("issues", []),
        },
    }
