from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from integrations.deerflow_bridge.bridge import DeerflowBridge

router = APIRouter(prefix="/api/v1/bridge/deerflow", tags=["deerflow-bridge"])

bridge = DeerflowBridge()


class BridgeInvokeRequest(BaseModel):
    skill_name: str
    payload: dict = {}


@router.get("/health")
async def bridge_health() -> dict:
    return bridge.health()


@router.get("/skills")
async def bridge_list_skills() -> dict:
    return bridge.list_skills()


@router.get("/memory")
async def bridge_memory() -> dict:
    return bridge.get_memory()


@router.post("/threads/{thread_id}/ensure")
async def bridge_ensure_thread(thread_id: str) -> dict:
    return bridge.ensure_thread(thread_id=thread_id)


@router.post("/invoke")
async def bridge_invoke(body: BridgeInvokeRequest) -> dict:
    return bridge.invoke_analysis_skill(body.skill_name, body.payload)
