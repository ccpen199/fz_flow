from __future__ import annotations

import json
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..services.config_transfer_service import config_transfer_service

router = APIRouter(prefix="/api/v1/config-transfer", tags=["config-transfer"])


class ConfigTransferRequest(BaseModel):
    bundle: dict[str, Any]
    target_scene_id: str | None = None
    mode: Literal["create", "merge", "replace"] = "create"


def _raise_transfer_error(exc: Exception) -> None:
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/scenes/{scene_id}/export")
async def export_scene_config(scene_id: str) -> Response:
    try:
        bundle = config_transfer_service.export_scene(scene_id)
    except ValueError as exc:
        _raise_transfer_error(exc)
    content = json.dumps(bundle, ensure_ascii=False, indent=2)
    scene_name = str(bundle.get("scene", {}).get("name") or scene_id).strip()
    safe_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in scene_name).strip("_")
    file_name = f"{safe_name or scene_id}-config.json"
    encoded_name = quote(file_name)
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{scene_id}-config.json"; filename*=UTF-8\'\'{encoded_name}',
        },
    )


@router.post("/preview")
async def preview_scene_config_import(body: ConfigTransferRequest) -> dict:
    try:
        return config_transfer_service.preview_import(
            body.bundle,
            target_scene_id=body.target_scene_id,
            mode=body.mode,
        )
    except (TypeError, ValueError) as exc:
        _raise_transfer_error(exc)


@router.post("/import")
async def import_scene_config(body: ConfigTransferRequest) -> dict:
    try:
        return config_transfer_service.import_bundle(
            body.bundle,
            target_scene_id=body.target_scene_id,
            mode=body.mode,
        )
    except (TypeError, ValueError) as exc:
        _raise_transfer_error(exc)
