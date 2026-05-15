from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from ..store import clear_runtime_state, persist_state

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class ResetRuntimeRequest(BaseModel):
    clear_state_file: bool = True
    clear_artifact_files: bool = True


@router.post("/runtime/reset", response_model=dict)
async def reset_runtime(body: ResetRuntimeRequest) -> dict:
    removed_artifact_files = 0
    if body.clear_artifact_files:
        artifact_root = Path("runtime_data/artifacts")
        if artifact_root.exists():
            for artifact_file in artifact_root.iterdir():
                if artifact_file.is_file():
                    artifact_file.unlink()
                    removed_artifact_files += 1

    clear_runtime_state(clear_state_file=body.clear_state_file)
    persist_state()
    return {
        "ok": True,
        "cleared": {
            "runtime_state": True,
            "state_file": body.clear_state_file,
            "artifact_files": removed_artifact_files,
        },
    }
