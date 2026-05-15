from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..store import DECKS, QUERY_PLANS, QUERY_RUNS, SESSIONS, SLIDE_DRAFTS, persist_state
from ..services.slide_service import build_slide_from_query, list_ppt_schemes, normalize_ppt_scheme
from packages.shared_contracts.python_models import DeckDTO, SessionStatus, SlideDraftDTO

router = APIRouter(prefix="/api/v1/analysis/sessions", tags=["slides"])


class RegenerateSlideRequest(BaseModel):
    style_hint: str = ""
    structure_hint: str = ""
    scheme: str = "presenton_ai"


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


def _slide_scheme(slide: SlideDraftDTO | None) -> str:
    if slide is None:
        return "presenton_ai"
    raw_scheme = str(
        slide.lineage_summary.get("ppt_scheme")
        or slide.chart_spec.get("ppt_scheme")
        or ""
    ).strip().lower()
    return raw_scheme if raw_scheme else "presenton_ai"


def _ensure_slide(session_id: str, *, scheme: str = "presenton_ai") -> SlideDraftDTO:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    scheme_key = normalize_ppt_scheme(scheme)
    slide = SLIDE_DRAFTS.get(session_id)
    if slide is None or _slide_scheme(slide) != scheme_key:
        query_plan = QUERY_PLANS.get(session_id)
        session_runs = [run for run in QUERY_RUNS.values() if run.session_id == session_id]
        slide = build_slide_from_query(
            session_id=session_id,
            global_goal=session.global_goal,
            status=session.status,
            query_plan=query_plan,
            query_run=session_runs[-1] if session_runs else None,
            scheme=scheme_key,
        )
        _store_slide(session_id, slide)
        session.status = SessionStatus.WAITING_SLIDE_REVIEW
        persist_state()
    return slide


@router.get("/-/ppt-schemes", response_model=list[dict])
async def get_ppt_schemes() -> list[dict]:
    return list_ppt_schemes()


@router.get("/{session_id}/current-slide", response_model=SlideDraftDTO)
async def get_current_slide(session_id: str, scheme: str = Query(default="presenton_ai")) -> SlideDraftDTO:
    return _ensure_slide(session_id, scheme=scheme)


@router.post("/{session_id}/current-slide/regenerate", response_model=SlideDraftDTO)
async def regenerate_current_slide(session_id: str, body: RegenerateSlideRequest) -> SlideDraftDTO:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    query_plan = QUERY_PLANS.get(session_id)
    session_runs = [run for run in QUERY_RUNS.values() if run.session_id == session_id]
    scheme_key = normalize_ppt_scheme(body.scheme)
    slide = build_slide_from_query(
        session_id=session_id,
        global_goal=session.global_goal,
        status=session.status,
        query_plan=query_plan,
        query_run=session_runs[-1] if session_runs else None,
        scheme=scheme_key,
    )
    suffix = " / ".join([part for part in [body.style_hint, body.structure_hint] if part.strip()])
    if suffix:
        slide.narrative = f"{slide.narrative} 已按要求重生成：{suffix}"
    else:
        slide.narrative = f"{slide.narrative} 已执行一次重生成。"
    slide.status = "pending_review"
    slide.version += 1
    _store_slide(session_id, slide)
    persist_state()
    return slide


@router.patch("/{session_id}/current-slide", response_model=SlideDraftDTO)
async def update_current_slide(session_id: str, body: UpdateSlideRequest) -> SlideDraftDTO:
    slide = _current_or_new_slide(session_id)
    payload = body.model_dump(exclude_none=True)
    for key, value in payload.items():
        setattr(slide, key, value)
    slide.version += 1
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
    retained_slide_ids: list[str] = []
    for slide_id in deck.slide_ids:
        existing_slide = SLIDE_DRAFTS.get(slide_id)
        if existing_slide is None:
            continue
        same_query = slide.query_id and existing_slide.query_id == slide.query_id
        same_session_without_query = not slide.query_id and existing_slide.session_id == session_id
        if same_query or same_session_without_query:
            continue
        retained_slide_ids.append(slide_id)
    deck.slide_ids = retained_slide_ids
    if slide.slide_id not in deck.slide_ids:
        deck.slide_ids.append(slide.slide_id)
    DECKS[deck_id] = deck
    session.status = SessionStatus.UPDATING_DECK
    session.updated_at = datetime.now(UTC)
    persist_state()
    return deck
