from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..store import DECKS, QUERY_PLANS, QUERY_RUNS, SESSIONS, SLIDE_DRAFTS, persist_state
from ..services.presentation_workflow_service import presentation_workflow_service
from ..services.slide_service import build_slide_from_query
from packages.shared_contracts.python_models import DeckDTO, SessionStatus, SlideDraftDTO

router = APIRouter(prefix="/api/v1/analysis/sessions", tags=["slides"])


class RegenerateSlideRequest(BaseModel):
    style_hint: str = ""
    structure_hint: str = ""


class UpdateSlideRequest(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    chart_spec: dict | None = None
    narrative: str | None = None
    findings: list[str] | None = None
    recommendations: list[str] | None = None


def _store_slide(session_id: str, slide: SlideDraftDTO) -> None:
    SLIDE_DRAFTS[session_id] = slide
    SLIDE_DRAFTS[slide.slide_id] = slide


def _current_or_new_slide(session_id: str) -> SlideDraftDTO:
    slide = SLIDE_DRAFTS.get(session_id)
    if slide is not None:
        return slide
    return _ensure_slide(session_id)


def _ensure_slide(session_id: str) -> SlideDraftDTO:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    slide = SLIDE_DRAFTS.get(session_id)
    if slide is None:
        query_plan = QUERY_PLANS.get(session_id)
        session_runs = [run for run in QUERY_RUNS.values() if run.session_id == session_id]
        slide = build_slide_from_query(
            session_id=session_id,
            global_goal=session.global_goal,
            status=session.status,
            query_plan=query_plan,
            query_run=session_runs[-1] if session_runs else None,
        )
        _store_slide(session_id, slide)
        session.status = SessionStatus.WAITING_SLIDE_REVIEW
        persist_state()
    return slide


@router.get("/{session_id}/current-slide", response_model=SlideDraftDTO)
async def get_current_slide(session_id: str) -> SlideDraftDTO:
    return _ensure_slide(session_id)


@router.post("/{session_id}/current-slide/regenerate", response_model=SlideDraftDTO)
async def regenerate_current_slide(session_id: str, body: RegenerateSlideRequest) -> SlideDraftDTO:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    query_plan = QUERY_PLANS.get(session_id)
    session_runs = [run for run in QUERY_RUNS.values() if run.session_id == session_id]
    current_slide = _current_or_new_slide(session_id)
    generated_slide = build_slide_from_query(
        session_id=session_id,
        global_goal=session.global_goal,
        status=session.status,
        query_plan=query_plan,
        query_run=session_runs[-1] if session_runs else None,
    )
    suffix = " / ".join([part for part in [body.style_hint, body.structure_hint] if part.strip()])
    if suffix:
        generated_slide.narrative = f"{generated_slide.narrative} 已按要求重生成：{suffix}"
    else:
        generated_slide.narrative = f"{generated_slide.narrative} 已执行一次重生成。"
    patch = {
        "title": generated_slide.title,
        "subtitle": generated_slide.subtitle,
        "chart_type": generated_slide.chart_spec.get("chart_type"),
        "findings": generated_slide.findings,
        "narrative": generated_slide.narrative,
        "recommendations": generated_slide.recommendations,
    }
    deck = DECKS.get(session.deck_id) if session.deck_id else None
    if deck is not None and current_slide.slide_id in deck.slide_ids:
        changed, _ = presentation_workflow_service.apply_revision(
            deck=deck,
            target_slide_ids=[current_slide.slide_id],
            patch=patch,
            turn_id=None,
            change_summary="从当前 SQL 结果重新生成页面",
        )
        slide = changed[0]
        _store_slide(session_id, slide)
        DECKS[deck.deck_id] = deck
    else:
        slide = presentation_workflow_service.clone_slide(current_slide, patch)
        _store_slide(session_id, slide)
        presentation_workflow_service.record_slide_revision(
            slide=slide,
            deck_id=None,
            turn_id=None,
            change_summary="从当前 SQL 结果重新生成页面",
        )
    persist_state()
    return slide


@router.patch("/{session_id}/current-slide", response_model=SlideDraftDTO)
async def update_current_slide(session_id: str, body: UpdateSlideRequest) -> SlideDraftDTO:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    source_slide = _current_or_new_slide(session_id)
    payload = body.model_dump(exclude_none=True)
    chart_spec = payload.pop("chart_spec", None)
    if isinstance(chart_spec, dict) and isinstance(chart_spec.get("chart_type"), str):
        payload["chart_type"] = chart_spec["chart_type"]
    deck = DECKS.get(session.deck_id) if session.deck_id else None
    if deck is not None and source_slide.slide_id in deck.slide_ids:
        changed, _ = presentation_workflow_service.apply_revision(
            deck=deck,
            target_slide_ids=[source_slide.slide_id],
            patch=payload,
            turn_id=None,
            change_summary="人工编辑当前页面",
        )
        slide = changed[0]
        DECKS[deck.deck_id] = deck
    else:
        slide = presentation_workflow_service.clone_slide(source_slide, payload)
        presentation_workflow_service.record_slide_revision(
            slide=slide,
            deck_id=None,
            turn_id=None,
            change_summary="人工编辑当前页面",
        )
    _store_slide(session_id, slide)
    persist_state()
    return slide


@router.post("/{session_id}/current-slide/approve", response_model=DeckDTO)
async def approve_current_slide(session_id: str) -> DeckDTO:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    slide = _current_or_new_slide(session_id)
    slide.status = "approved"
    _store_slide(session_id, slide)

    deck_id = session.deck_id or f"deck_{uuid4().hex[:10]}"
    session.deck_id = deck_id

    deck = DECKS.get(deck_id)
    if deck is None:
        deck = DeckDTO(
            deck_id=deck_id,
            session_id=session_id,
            title=f"Deck for {session.global_goal[:30]}",
            slide_ids=[],
            status="building",
        )
    presentation_workflow_service.append_slide(
        deck=deck,
        slide=slide,
        turn_id=None,
        change_summary="人工确认页面并加入 Deck",
    )
    DECKS[deck_id] = deck
    session.status = SessionStatus.UPDATING_DECK
    session.updated_at = datetime.now(UTC)
    persist_state()
    return deck
