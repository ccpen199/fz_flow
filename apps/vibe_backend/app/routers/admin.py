from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..store import clear_runtime_state, persist_state

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class ResetRuntimeRequest(BaseModel):
    clear_state_file: bool = True


@router.post("/runtime/reset", response_model=dict)
async def reset_runtime(body: ResetRuntimeRequest) -> dict:
    clear_runtime_state(clear_state_file=body.clear_state_file)
    persist_state()
    return {
        "ok": True,
        "cleared": {
            "runtime_state": True,
            "state_file": body.clear_state_file,
        },
    }
