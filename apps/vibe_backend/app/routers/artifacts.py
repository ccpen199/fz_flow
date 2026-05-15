from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from ..store import ARTIFACTS
from packages.shared_contracts.python_models import ArtifactDTO

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}", response_model=ArtifactDTO)
async def get_artifact(artifact_id: str) -> ArtifactDTO:
    artifact = ARTIFACTS.get(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return artifact


@router.get("/{artifact_id}/download")
async def download_artifact(artifact_id: str) -> Response:
    artifact = ARTIFACTS.get(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    if artifact.local_path:
        path = Path(artifact.local_path)
        if path.exists():
            return FileResponse(path, filename=artifact.file_name, media_type="application/octet-stream")
    body = f"stub artifact content for {artifact.file_name}".encode("utf-8")
    headers = {
        "Content-Disposition": f'attachment; filename="{artifact.file_name}"',
    }
    return Response(
        content=body,
        media_type="application/octet-stream",
        headers=headers,
    )
