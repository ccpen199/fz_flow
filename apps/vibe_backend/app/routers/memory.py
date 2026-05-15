from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel

from ..store import MEMORY_ENTRIES, persist_state
from packages.shared_contracts.python_models import MemoryEntryDTO

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


class CreateMemoryEntryRequest(BaseModel):
    topic: str
    content: str
    tags: list[str] = []


@router.get("", response_model=list[MemoryEntryDTO])
async def list_memory_entries() -> list[MemoryEntryDTO]:
    return list(MEMORY_ENTRIES.values())


@router.post("", response_model=MemoryEntryDTO)
async def create_memory_entry(body: CreateMemoryEntryRequest) -> MemoryEntryDTO:
    entry = MemoryEntryDTO(
        memory_id=f"mem_{uuid4().hex[:10]}",
        topic=body.topic.strip(),
        content=body.content.strip(),
        tags=body.tags,
    )
    MEMORY_ENTRIES[entry.memory_id] = entry
    persist_state()
    return entry
