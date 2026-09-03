from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.input_correction_lexicon_service import input_correction_lexicon_service


router = APIRouter(prefix="/api/v1/input-corrections", tags=["input-corrections"])


class InputCorrectionUpsertRequest(BaseModel):
    correct_word: str = Field(..., min_length=1, max_length=255)
    note: str = Field(default="", max_length=500)


class InputCorrectionPatchRequest(BaseModel):
    enabled: bool


@router.get("")
async def list_input_corrections(include_disabled: bool = False) -> dict:
    items = input_correction_lexicon_service.list_words(include_disabled=include_disabled)
    return {"ok": True, "items": items, "count": len(items)}


@router.post("")
async def upsert_input_correction(body: InputCorrectionUpsertRequest) -> dict:
    try:
        item = input_correction_lexicon_service.upsert_word(
            body.correct_word,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"input correction upsert failed: {exc}") from exc
    return {"ok": True, "item": item}


@router.patch("/{correction_id}")
async def patch_input_correction(correction_id: str, body: InputCorrectionPatchRequest) -> dict:
    try:
        item = input_correction_lexicon_service.set_enabled(correction_id, body.enabled)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"input correction update failed: {exc}") from exc
    if item is None:
        raise HTTPException(status_code=404, detail="input correction not found")
    return {"ok": True, "item": item}


@router.delete("/{correction_id}")
async def delete_input_correction(correction_id: str) -> dict:
    try:
        deleted = input_correction_lexicon_service.delete_word(correction_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"input correction delete failed: {exc}") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="input correction not found")
    return {"ok": True, "correction_id": correction_id}
