from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.llm_agent_service import LlmAgentService
from ..services.scene_cache_service import scene_cache_service
from ..store import persist_state

router = APIRouter(prefix="/api/v1/llm-agent", tags=["llm-agent"])
service = LlmAgentService()


class RecommendRequest(BaseModel):
    goal: str = ""
    max_tables: int = Field(default=4, ge=1, le=20)
    max_fields_per_table: int = Field(default=12, ge=1, le=50)


class ApplyDraftRequest(BaseModel):
    merge_mode: str = Field(default="append", description="append|replace")
    recommendation: dict | None = None


class ValidateDraftRequest(BaseModel):
    recommendation: dict | None = None


class PublishDraftRequest(BaseModel):
    recommendation: dict | None = None


def _require_recommendation(recommendation: dict | None) -> dict:
    if not recommendation:
        raise HTTPException(status_code=422, detail="recommendation is required")
    return recommendation


@router.get("/health")
async def llm_agent_health() -> dict:
    return service.health()


@router.get("/cache")
async def llm_agent_cache_status() -> dict:
    return service.schema_cache_status()


@router.post("/cache/refresh")
async def refresh_llm_agent_cache() -> dict:
    return service.refresh_schema_cache()


@router.post("/scenes/{scene_id}/recommend")
async def recommend_scene_playbook(scene_id: str, body: RecommendRequest) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    recommendation = service.recommend(
        scene=scene,
        goal=body.goal.strip(),
        max_tables=body.max_tables,
        max_fields_per_table=body.max_fields_per_table,
    )
    return recommendation


@router.get("/scenes/{scene_id}/draft")
async def get_scene_draft(scene_id: str) -> dict:
    raise HTTPException(
        status_code=410,
        detail="draft persistence is removed; send recommendation in validate/apply/publish request body",
    )


@router.post("/scenes/{scene_id}/validate")
async def validate_scene_draft(scene_id: str, body: ValidateDraftRequest) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    draft = _require_recommendation(body.recommendation)
    validation = service.validate_recommendation(scene=scene, recommendation=draft)
    canonical = validation.get("canonical_recommendation", {})
    canonical["last_validate_result"] = {
        "ok": validation.get("ok", False),
        "error_count": validation.get("error_count", 0),
        "warning_count": validation.get("warning_count", 0),
        "issues": validation.get("issues", []),
    }
    return {
        "ok": validation.get("ok", False),
        "scene_id": scene_id,
        "recommendation_id": canonical.get("recommendation_id"),
        "error_count": validation.get("error_count", 0),
        "warning_count": validation.get("warning_count", 0),
        "issues": validation.get("issues", []),
        "draft": canonical,
    }


@router.post("/scenes/{scene_id}/apply")
async def apply_scene_draft(scene_id: str, body: ApplyDraftRequest) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    draft = _require_recommendation(body.recommendation)

    merge_mode = body.merge_mode.strip().lower()
    if merge_mode not in {"append", "replace"}:
        raise HTTPException(status_code=422, detail="merge_mode must be append or replace")

    validation = service.validate_recommendation(scene=scene, recommendation=draft)
    if not validation.get("ok", False):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "draft validation failed",
                "issues": validation.get("issues", []),
            },
        )

    canonical = validation.get("canonical_recommendation", {})
    apply_result = service.apply_to_scene(scene=scene, recommendation=canonical, merge_mode=merge_mode)
    canonical["last_validate_result"] = {
        "ok": True,
        "error_count": validation.get("error_count", 0),
        "warning_count": validation.get("warning_count", 0),
        "issues": validation.get("issues", []),
    }
    canonical["last_apply_result"] = apply_result
    scene_cache_service.upsert_scene(scene)
    persist_state()
    return {
        "ok": True,
        "scene_id": scene_id,
        "recommendation_id": canonical.get("recommendation_id"),
        "validate_result": {
            "ok": True,
            "error_count": validation.get("error_count", 0),
            "warning_count": validation.get("warning_count", 0),
        },
        "apply_result": apply_result,
    }


@router.post("/scenes/{scene_id}/publish")
async def publish_scene_from_llm_agent(scene_id: str, body: PublishDraftRequest) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    draft = _require_recommendation(body.recommendation)
    if not isinstance(draft.get("last_apply_result"), dict):
        raise HTTPException(status_code=409, detail="draft must be applied before publish")

    validation = service.validate_recommendation(scene=scene, recommendation=draft)
    if not validation.get("ok", False):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "draft validation failed",
                "issues": validation.get("issues", []),
            },
        )

    canonical = validation.get("canonical_recommendation", {})
    scene.version += 1
    scene_cache_service.upsert_scene(scene)
    canonical["scene_version"] = scene.version
    canonical["last_validate_result"] = {
        "ok": True,
        "error_count": validation.get("error_count", 0),
        "warning_count": validation.get("warning_count", 0),
        "issues": validation.get("issues", []),
    }
    canonical["last_publish_result"] = {
        "ok": True,
        "scene_id": scene_id,
        "scene_version": scene.version,
        "published_by": "llm-agent-module",
    }
    persist_state()
    return canonical["last_publish_result"]
