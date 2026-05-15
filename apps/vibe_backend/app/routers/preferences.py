from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..store import PREFERENCES, persist_state
from packages.shared_contracts.python_models import PreferenceDTO

router = APIRouter(prefix="/api/v1/preferences", tags=["preferences"])


class UpdatePreferenceRequest(BaseModel):
    ppt_tone: str | None = None
    ppt_template: str | None = None
    preferred_chart_types: list[str] | None = None
    default_language: str | None = None


@router.get("", response_model=PreferenceDTO)
async def get_preferences() -> PreferenceDTO:
    return PREFERENCES["default_user"]


@router.patch("", response_model=PreferenceDTO)
async def update_preferences(body: UpdatePreferenceRequest) -> PreferenceDTO:
    pref = PREFERENCES["default_user"]
    payload = body.model_dump(exclude_none=True)
    for key, value in payload.items():
        setattr(pref, key, value)
    PREFERENCES["default_user"] = pref
    persist_state()
    return pref
