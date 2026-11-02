from __future__ import annotations

import os
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..services.presentation_workflow_service import presentation_workflow_service
from ..store import DECK_REVISIONS, DECKS, QUERY_RUNS, SESSIONS, SLIDE_DRAFTS, persist_state
from packages.shared_contracts.python_models import DeckDTO, DeckRevisionDTO

router = APIRouter(prefix="/api/v1/decks", tags=["decks"])


class ReorderDeckRequest(BaseModel):
    slide_ids: list[str]


class RestoreDeckRevisionRequest(BaseModel):
    note: str = ""


class TrainPPTAgentCallbackRequest(BaseModel):
    base_revision_id: str | None = None
    editor_document: dict
    message: str = ""


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
    requested = body.slide_ids
    if len(requested) != len(set(requested)) or set(requested) != set(deck.slide_ids):
        raise HTTPException(status_code=400, detail="slide_ids must be a unique reordering of current deck slides")
    deck.slide_ids = requested
    deck.editor_document = None
    presentation_workflow_service.record_deck_revision(
        deck=deck,
        turn_id=None,
        change_summary="人工调整页面顺序",
    )
    DECKS[deck_id] = deck
    persist_state()
    return deck


@router.get("/{deck_id}/revisions", response_model=list[DeckRevisionDTO])
async def list_deck_revisions(deck_id: str) -> list[DeckRevisionDTO]:
    deck = DECKS.get(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="deck not found")
    return presentation_workflow_service.list_deck_revisions(deck_id)


@router.get("/{deck_id}/revisions/{revision_id}", response_model=dict)
async def get_deck_revision(deck_id: str, revision_id: str) -> dict:
    revision = DECK_REVISIONS.get(revision_id)
    if revision is None or revision.deck_id != deck_id:
        raise HTTPException(status_code=404, detail="deck revision not found")
    slides = [
        SLIDE_DRAFTS[slide_id].model_dump(mode="json")
        for slide_id in revision.slide_ids
        if slide_id in SLIDE_DRAFTS
    ]
    return {"revision": revision.model_dump(mode="json"), "slides": slides}


@router.post("/{deck_id}/revisions/{revision_id}/restore", response_model=DeckDTO)
async def restore_deck_revision(deck_id: str, revision_id: str, body: RestoreDeckRevisionRequest) -> DeckDTO:
    deck = DECKS.get(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="deck not found")
    try:
        presentation_workflow_service.restore_deck_revision(
            deck=deck,
            target_revision_id=revision_id,
            turn_id=None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.note.strip():
        latest = DECK_REVISIONS.get(deck.current_revision_id or "")
        if latest is not None:
            latest.change_summary = f"{latest.change_summary}：{body.note.strip()[:200]}"
    DECKS[deck_id] = deck
    session = SESSIONS.get(deck.session_id)
    if session is not None:
        session.updated_at = datetime.now(UTC)
    persist_state()
    return deck


@router.post("/{deck_id}/revisions/{revision_id}/switch", response_model=DeckDTO)
async def switch_deck_revision(deck_id: str, revision_id: str) -> DeckDTO:
    deck = DECKS.get(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="deck not found")
    try:
        presentation_workflow_service.switch_deck_revision(
            deck=deck,
            target_revision_id=revision_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    DECKS[deck_id] = deck
    session = SESSIONS.get(deck.session_id)
    if session is not None:
        session.updated_at = datetime.now(UTC)
    persist_state()
    return deck


@router.get("/{deck_id}/train-ppt-agent-handoff", response_model=dict)
async def get_train_ppt_agent_handoff(deck_id: str, request: Request) -> dict:
    deck = DECKS.get(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="deck not found")
    editor_document = deck.editor_document or presentation_workflow_service.build_editor_document(deck)
    slides = []
    for index, slide_id in enumerate(deck.slide_ids, start=1):
        slide = SLIDE_DRAFTS.get(slide_id)
        if slide is None:
            continue
        query_run = QUERY_RUNS.get(slide.query_id or "")
        slides.append(
            {
                "index": index,
                "slide_id": slide.slide_id,
                "query_id": slide.query_id,
                "editable": {
                    "title": slide.title,
                    "subtitle": slide.subtitle,
                    "chart_spec": slide.chart_spec,
                    "findings": slide.findings,
                    "narrative": slide.narrative,
                    "recommendations": slide.recommendations,
                    "style_tokens": deck.style_tokens,
                },
                "lineage": {
                    "query_id": slide.query_id,
                    "sql_summary": query_run.sql_explanation if query_run else "",
                    "sql": query_run.sql if query_run else "",
                    "result_rows": query_run.rows_count if query_run else 0,
                    "lineage_summary": slide.lineage_summary,
                },
            }
        )
    public_base_url = os.getenv("FZ_WORKFLOW_PUBLIC_URL", str(request.base_url).rstrip("/")).rstrip("/")
    handoff_url = f"{public_base_url}/api/v1/decks/{deck.deck_id}/train-ppt-agent-handoff"
    callback_url = f"{public_base_url}/api/v1/decks/{deck.deck_id}/train-ppt-agent/callback"
    editor_base_url = os.getenv("TRAIN_PPT_AGENT_EDITOR_URL", "http://127.0.0.1:5173/editor")
    editor_url = f"{editor_base_url}{'&' if '?' in editor_base_url else '?'}fz_handoff_url={quote(handoff_url, safe='')}"
    persist_state()
    return {
        "schema_version": "fz-workflow.train-ppt-agent-handoff/v2",
        "editor_url": editor_url,
        "editor_base_url": editor_base_url,
        "handoff_url": handoff_url,
        "callback_url": callback_url,
        "base_revision_id": deck.current_revision_id,
        "deck": deck.model_dump(mode="json"),
        "current_revision": (
            DECK_REVISIONS[deck.current_revision_id].model_dump(mode="json")
            if deck.current_revision_id in DECK_REVISIONS
            else None
        ),
        "slides": slides,
        "editor_document": editor_document,
        "callback_contract": {
            "required": ["deck_id", "base_revision_id", "slides"],
            "rule": "画布回传必须由 fz_workflow 创建新的 DeckRevision，不能覆盖已有版本。",
        },
    }


@router.post("/{deck_id}/train-ppt-agent/callback", response_model=dict)
async def receive_train_ppt_agent_callback(deck_id: str, body: TrainPPTAgentCallbackRequest, request: Request) -> dict:
    deck = DECKS.get(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="deck not found")
    try:
        changed_slides, revision = presentation_workflow_service.apply_editor_document(
            deck=deck,
            editor_document=body.editor_document,
            base_revision_id=body.base_revision_id,
            turn_id=None,
            change_summary=body.message.strip()[:200] or "TrainPPTAgent 画布同步",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409 if "stale" in str(exc) else 400, detail=str(exc)) from exc
    DECKS[deck_id] = deck
    session = SESSIONS.get(deck.session_id)
    if session is not None:
        session.updated_at = datetime.now(UTC)
    persist_state()
    return {
        "ok": True,
        "deck": deck.model_dump(mode="json"),
        "deck_revision": revision.model_dump(mode="json"),
        "slide_ids": [slide.slide_id for slide in changed_slides],
        "editor_document": deck.editor_document,
        "next_handoff_url": f"{os.getenv('FZ_WORKFLOW_PUBLIC_URL', str(request.base_url).rstrip('/')).rstrip('/')}/api/v1/decks/{deck_id}/train-ppt-agent-handoff",
    }
