from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..services.semantic_field_cache_service import semantic_field_cache_service
from ..services.scene_cache_service import scene_cache_service

router = APIRouter(prefix="/api/v1/semantic-cache", tags=["semantic-cache"])


class SemanticFieldUpsertRequest(BaseModel):
    semantic_name: str = Field(..., min_length=1)
    semantic_definition: str = ""
    aliases: list[str] = Field(default_factory=list)
    unit: str = ""
    aggregation: str = ""
    table_name: str = Field(..., min_length=1)
    field_name: str = Field(..., min_length=1)
    er_path: str = ""
    role: Literal["metric", "dimension", "time", "filter"] = "dimension"
    zone: Literal["modeled", "effective"] = "modeled"
    enabled: bool = True


class SemanticFieldPatchRequest(BaseModel):
    semantic_name: str | None = None
    semantic_definition: str | None = None
    aliases: list[str] | None = None
    unit: str | None = None
    aggregation: str | None = None
    table_name: str | None = None
    field_name: str | None = None
    er_path: str | None = None
    role: Literal["metric", "dimension", "time", "filter"] | None = None
    zone: Literal["modeled", "effective"] | None = None
    enabled: bool | None = None


@router.get("/scenes/{scene_id}/fields")
async def list_scene_semantic_fields(
    scene_id: str,
    zone: str = Query(default="all", pattern="^(all|modeled|effective)$"),
    include_disabled: bool = False,
) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    # 当语义缓存为空时，用场景字段做一次默认回填，避免前端初始为空。
    if zone in {"all", "modeled"}:
        semantic_field_cache_service.seed_modeled_from_scene_fields(scene_id, scene.fields)

    rows = semantic_field_cache_service.list_scene_fields(
        scene_id,
        zone=zone,
        include_disabled=include_disabled,
    )
    return {
        "scene_id": scene_id,
        "zone": zone,
        "include_disabled": include_disabled,
        "count": len(rows),
        "fields": rows,
    }


@router.post("/scenes/{scene_id}/fields")
async def add_scene_semantic_field(scene_id: str, body: SemanticFieldUpsertRequest) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")
    try:
        row = semantic_field_cache_service.upsert_field(
            scene_id,
            {
                "semantic_name": body.semantic_name,
                "semantic_definition": body.semantic_definition,
                "aliases": body.aliases,
                "unit": body.unit,
                "aggregation": body.aggregation,
                "table_name": body.table_name,
                "field_name": body.field_name,
                "er_path": body.er_path,
                "role": body.role,
                "zone": body.zone,
                "enabled": body.enabled,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"semantic cache upsert failed: {exc}") from exc
    return row


@router.patch("/scenes/{scene_id}/fields/{cache_id}")
async def patch_scene_semantic_field(scene_id: str, cache_id: str, body: SemanticFieldPatchRequest) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    rows = semantic_field_cache_service.list_scene_fields(scene_id, zone="all", include_disabled=True)
    current = next((row for row in rows if row.get("cache_id") == cache_id), None)
    if not current:
        raise HTTPException(status_code=404, detail="semantic cache field not found")

    payload = {
        "semantic_name": body.semantic_name if body.semantic_name is not None else current.get("semantic_name", ""),
        "semantic_definition": (
            body.semantic_definition if body.semantic_definition is not None else current.get("semantic_definition", "")
        ),
        "aliases": body.aliases if body.aliases is not None else current.get("aliases", []),
        "unit": body.unit if body.unit is not None else current.get("unit", ""),
        "aggregation": body.aggregation if body.aggregation is not None else current.get("aggregation", ""),
        "table_name": body.table_name if body.table_name is not None else current.get("table_name", ""),
        "field_name": body.field_name if body.field_name is not None else current.get("field_name", ""),
        "er_path": body.er_path if body.er_path is not None else current.get("er_path", ""),
        "role": body.role if body.role is not None else current.get("role", "dimension"),
        "zone": body.zone if body.zone is not None else current.get("zone", "modeled"),
        "enabled": body.enabled if body.enabled is not None else bool(current.get("enabled", True)),
    }

    try:
        row = semantic_field_cache_service.upsert_field(scene_id, payload, cache_id=cache_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"semantic cache patch failed: {exc}") from exc
    return row


@router.delete("/scenes/{scene_id}/fields/{cache_id}")
async def delete_scene_semantic_field(scene_id: str, cache_id: str) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")
    deleted = semantic_field_cache_service.delete_field(scene_id, cache_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="semantic cache field not found")
    return {"ok": True, "scene_id": scene_id, "cache_id": cache_id}
