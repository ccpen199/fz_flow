from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.artifact_service import export_deck_artifact
from ..store import ARTIFACTS, DECKS, SESSIONS, SLIDE_DRAFTS, persist_state
from packages.shared_contracts.python_models import ArtifactDTO, DeckDTO

router = APIRouter(prefix="/api/v1/decks", tags=["decks"])


class ReorderDeckRequest(BaseModel):
    slide_ids: list[str]


@router.get("/{deck_id}", response_model=DeckDTO)
async def get_deck(deck_id: str) -> DeckDTO:
    deck = DECKS.get(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="deck not found")
    return deck


@router.post("/{deck_id}/reorder", response_model=DeckDTO)
async def reorder_deck(deck_id: str, body: ReorderDeckRequest) -> DeckDTO:
    deck = DECKS.get(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="deck not found")
    deck.slide_ids = body.slide_ids
    DECKS[deck_id] = deck
    persist_state()
    return deck


@router.post("/{deck_id}/export", response_model=ArtifactDTO)
async def export_deck(deck_id: str) -> ArtifactDTO:
    deck = DECKS.get(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="deck not found")
    slide_by_id = {slide.slide_id: slide for slide in SLIDE_DRAFTS.values()}
    slides = [slide_by_id[slide_id] for slide_id in deck.slide_ids if slide_id in slide_by_id]
    artifact = export_deck_artifact(deck, slides=slides)
    ARTIFACTS[artifact.artifact_id] = artifact
    deck.status = "exported"
    DECKS[deck_id] = deck
    session = SESSIONS.get(deck.session_id)
    if session is not None:
        session.updated_at = datetime.now(UTC)
    persist_state()
    return artifact
