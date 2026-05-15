from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from uuid import uuid4

from ..services.scene_cache_service import scene_cache_service
from ..store import ARTIFACTS, DECKS, DEERFLOW_CHAT_TURNS, QUERY_PLANS, QUERY_RUNS, SESSIONS, SLIDE_DRAFTS, persist_state
from ..services.query_service import build_query_plan
from ..services.runtime_persistence_service import runtime_persistence_service
from ..services.sql_result_agent_service import SqlResultAgentService
from integrations.deerflow_bridge.bridge import DeerflowBridge
from packages.analysis_domain.models import AnalysisSession
from packages.shared_contracts.python_models import (
    AnalysisSessionDTO,
    ArtifactDTO,
    DeckDTO,
    DeerflowChatTurnDTO,
    QueryPlanDTO,
    QueryRunDTO,
    SessionStatus,
    SlideDraftDTO,
)

router = APIRouter(prefix="/api/v1/analysis/sessions", tags=["analysis-sessions"])
bridge = DeerflowBridge()
sql_result_agent_service = SqlResultAgentService()


class CreateAnalysisSessionRequest(BaseModel):
    scene_id: str
    global_goal: str = Field(..., min_length=1)


class UpdateGoalRequest(BaseModel):
    global_goal: str = Field(..., min_length=1)


class SessionDeerflowChatRequest(BaseModel):
    message: str = Field(..., min_length=1)


def _dump_dto(value) -> dict | None:
    if value is None:
        return None
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _slide_for_session(session_id: str):
    slide = SLIDE_DRAFTS.get(session_id)
    if slide is not None:
        return slide
    for candidate in SLIDE_DRAFTS.values():
        if getattr(candidate, "session_id", None) == session_id:
            return candidate
    return None


def _deck_for_session(session) -> object | None:
    if session.deck_id:
        deck = DECKS.get(session.deck_id)
        if deck is not None:
            return deck
    for deck in DECKS.values():
        if deck.session_id == session.session_id:
            return deck
    return None


def _latest_artifact_for_session(session_id: str, deck_id: str | None = None):
    matches = [
        artifact
        for artifact in ARTIFACTS.values()
        if artifact.session_id == session_id or (deck_id and artifact.deck_id == deck_id)
    ]
    return matches[-1] if matches else None


def _sync_session_from_saved_query(
    session: AnalysisSession,
    query_plan: QueryPlanDTO | None,
    query_run: QueryRunDTO | None,
) -> AnalysisSession:
    if query_run is not None:
        run_scene_id = str((query_run.lineage or {}).get("scene_id") or "").strip()
        if run_scene_id and session.scene_id != run_scene_id:
            session.scene_id = run_scene_id
        run_scene_version = str((query_run.lineage or {}).get("scene_version") or "").strip()
        if run_scene_version and session.scene_version != run_scene_version:
            session.scene_version = run_scene_version
        if query_run.status == "succeeded":
            session.status = SessionStatus.SUMMARIZING_RESULT
    if query_plan is not None and query_plan.intent and session.global_goal != query_plan.intent:
        session.global_goal = query_plan.intent
    return session


def _load_session_workflow_stages(session_id: str) -> bool:
    restored = False
    latest_slide_selected = session_id in SLIDE_DRAFTS
    latest_query_run: QueryRunDTO | None = None
    for row in runtime_persistence_service.list_workflow_stages(session_id=session_id):
        stage_key = str(row.get("stage_key") or "").strip()
        payload = row.get("stage") or {}
        if not stage_key or not payload:
            continue
        try:
            if stage_key == "session":
                session = AnalysisSession.from_dto(AnalysisSessionDTO.model_validate(payload))
                SESSIONS[session.session_id] = session
                restored = True
            elif stage_key == "query_plan":
                query_plan = QueryPlanDTO.model_validate(payload)
                QUERY_PLANS[query_plan.session_id] = query_plan
                restored = True
            elif stage_key.startswith("query_run:"):
                query_run = QueryRunDTO.model_validate(payload)
                QUERY_RUNS[query_run.query_id] = query_run
                if latest_query_run is None:
                    latest_query_run = query_run
                restored = True
            elif stage_key.startswith("slide:"):
                slide = SlideDraftDTO.model_validate(payload)
                SLIDE_DRAFTS[slide.slide_id] = slide
                if slide.session_id == session_id and not latest_slide_selected:
                    SLIDE_DRAFTS[session_id] = slide
                    latest_slide_selected = True
                restored = True
            elif stage_key.startswith("deck:"):
                deck = DeckDTO.model_validate(payload)
                DECKS[deck.deck_id] = deck
                session = SESSIONS.get(deck.session_id)
                if session is not None and not session.deck_id:
                    session.deck_id = deck.deck_id
                restored = True
            elif stage_key.startswith("artifact:"):
                artifact = ArtifactDTO.model_validate(payload)
                ARTIFACTS[artifact.artifact_id] = artifact
                restored = True
        except Exception as exc:  # noqa: BLE001
            print(f"[analysis_sessions] skip invalid saved stage {session_id}/{stage_key}: {exc}")
    session = SESSIONS.get(session_id)
    if session is not None:
        _sync_session_from_saved_query(session, QUERY_PLANS.get(session_id), latest_query_run)
    return restored


def _recover_session_from_persistence(session_id: str) -> AnalysisSession | None:
    session_key = str(session_id or "").strip()
    if not session_key:
        return None

    if session_key in SESSIONS:
        _load_session_workflow_stages(session_key)
        return SESSIONS[session_key]

    session_stage = runtime_persistence_service.get_workflow_stage(session_id=session_key, stage_key="session")
    if session_stage:
        try:
            session = AnalysisSession.from_dto(AnalysisSessionDTO.model_validate(session_stage))
            SESSIONS[session.session_id] = session
            _load_session_workflow_stages(session_key)
            persist_state()
            return session
        except Exception as exc:  # noqa: BLE001
            print(f"[analysis_sessions] session stage recovery failed {session_key}: {exc}")

    persisted = runtime_persistence_service.get_latest_query_result(session_key)
    query_plan = None
    query_run = None
    if persisted:
        plan_payload = persisted.get("query_plan") or None
        run_payload = persisted.get("query_run") or None
        try:
            if plan_payload:
                query_plan = QueryPlanDTO.model_validate(plan_payload)
                QUERY_PLANS[session_key] = query_plan
            if run_payload:
                query_run = QueryRunDTO.model_validate(run_payload)
                QUERY_RUNS[query_run.query_id] = query_run
        except Exception as exc:  # noqa: BLE001
            print(f"[analysis_sessions] query result recovery failed {session_key}: {exc}")

    if query_run is None:
        _load_session_workflow_stages(session_key)
        return SESSIONS.get(session_key)

    scene_id = str((query_run.lineage or {}).get("scene_id") or "scene_prd_price").strip()
    scene_version = str((query_run.lineage or {}).get("scene_version") or "scene_v1").strip()
    goal = str((query_plan.intent if query_plan is not None else "") or query_run.sql_explanation or query_run.sql).strip()
    session = AnalysisSession(
        session_id=session_key,
        scene_id=scene_id,
        scene_version=scene_version,
        deerflow_thread_id=f"vibe_{session_key}",
        global_goal=goal or "已保存 SQL 分析",
        status=SessionStatus.SUMMARIZING_RESULT if query_run.status == "succeeded" else SessionStatus.FAILED,
    )
    SESSIONS[session.session_id] = session
    _load_session_workflow_stages(session_key)
    _sync_session_from_saved_query(session, query_plan, query_run)
    persist_state()
    return session


@router.get("", response_model=list[AnalysisSessionDTO])
async def list_sessions() -> list[AnalysisSessionDTO]:
    sessions = sorted(SESSIONS.values(), key=lambda item: item.updated_at, reverse=True)
    return [session.to_dto() for session in sessions]


@router.post("", response_model=AnalysisSessionDTO)
async def create_session(body: CreateAnalysisSessionRequest) -> AnalysisSessionDTO:
    scene = scene_cache_service.get_scene(body.scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="scene not found")
    goal = body.global_goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="global_goal is required")

    session = AnalysisSession(scene_id=body.scene_id, global_goal=goal)
    session.scene_version = f"scene_v{scene.version}"
    session.deerflow_thread_id = f"vibe_{session.session_id}"
    session.status = SessionStatus.PLANNING
    bridge.ensure_thread(
        thread_id=session.deerflow_thread_id,
        metadata={
            "session_id": session.session_id,
            "scene_id": session.scene_id,
            "scene_version": session.scene_version,
            "global_goal": session.global_goal,
        },
    )
    SESSIONS[session.session_id] = session
    persist_state()
    return session.to_dto()


@router.delete("/{session_id}", response_model=dict)
async def delete_session(session_id: str) -> dict:
    saved_stage_count = 0
    session = SESSIONS.pop(session_id, None)
    if session is None:
        has_saved_query = runtime_persistence_service.get_latest_query_result(session_id) is not None
        saved_stage_count = len(runtime_persistence_service.list_workflow_stages(session_id=session_id))
        session = _recover_session_from_persistence(session_id)
        if session is not None:
            SESSIONS.pop(session_id, None)
        elif not has_saved_query and saved_stage_count == 0:
            raise HTTPException(status_code=404, detail="session not found")
    else:
        saved_stage_count = len(runtime_persistence_service.list_workflow_stages(session_id=session_id))

    QUERY_PLANS.pop(session_id, None)

    removed_query_ids = [query_id for query_id, run in QUERY_RUNS.items() if run.session_id == session_id]
    for query_id in removed_query_ids:
        QUERY_RUNS.pop(query_id, None)

    removed_slide_keys = [
        key
        for key, slide in SLIDE_DRAFTS.items()
        if key == session_id or getattr(slide, "session_id", None) == session_id
    ]
    for key in removed_slide_keys:
        SLIDE_DRAFTS.pop(key, None)

    removed_deck_ids = [deck_id for deck_id, deck in DECKS.items() if deck.session_id == session_id]
    for deck_id in removed_deck_ids:
        DECKS.pop(deck_id, None)

    removed_artifact_ids = [
        artifact_id
        for artifact_id, artifact in ARTIFACTS.items()
        if artifact.session_id == session_id or (artifact.deck_id and artifact.deck_id in removed_deck_ids)
    ]
    for artifact_id in removed_artifact_ids:
        ARTIFACTS.pop(artifact_id, None)

    removed_chat_turn_count = len(DEERFLOW_CHAT_TURNS.pop(session_id, []))
    persistence_delete = runtime_persistence_service.delete_query_results_for_session(session_id)
    persist_state()
    return {
        "ok": True,
        "session_id": session_id,
        "deleted": {
            "query_runs": len(removed_query_ids),
            "slide_drafts": len(removed_slide_keys),
            "decks": len(removed_deck_ids),
            "artifacts": len(removed_artifact_ids),
            "chat_turns": removed_chat_turn_count,
            "saved_stages": saved_stage_count,
            "persistent_query_rows": persistence_delete,
        },
    }


@router.get("/{session_id}", response_model=AnalysisSessionDTO)
async def get_session(session_id: str) -> AnalysisSessionDTO:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    return session.to_dto()


@router.get("/{session_id}/report-state", response_model=dict)
async def get_session_report_state(session_id: str) -> dict:
    session = SESSIONS.get(session_id)
    if not session:
        session = _recover_session_from_persistence(session_id)
    else:
        _load_session_workflow_stages(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    slide = _slide_for_session(session_id)
    deck = _deck_for_session(session)
    artifact = _latest_artifact_for_session(session_id, deck.deck_id if deck else session.deck_id)
    deck_slides = []
    if deck is not None:
        slide_by_id = {item.slide_id: item for item in SLIDE_DRAFTS.values()}
        deck_slides = [
            slide_by_id[slide_id].model_dump(mode="json")
            for slide_id in deck.slide_ids
            if slide_id in slide_by_id
        ]

    return {
        "ok": True,
        "session": session.to_dto().model_dump(mode="json"),
        "slide": _dump_dto(slide),
        "deck": _dump_dto(deck),
        "artifact": _dump_dto(artifact),
        "deck_slides": deck_slides,
        "has_saved_report": bool(slide or deck or artifact),
    }


@router.get("/{session_id}/thread-context", response_model=dict)
async def get_session_thread_context(session_id: str) -> dict:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    if not session.deerflow_thread_id:
        raise HTTPException(status_code=400, detail="session thread not initialized")
    return bridge.ensure_thread(
        thread_id=session.deerflow_thread_id,
        metadata={
            "session_id": session.session_id,
            "scene_id": session.scene_id,
            "scene_version": session.scene_version,
            "global_goal": session.global_goal,
        },
    )


@router.get("/{session_id}/deerflow/chat-turns", response_model=list[DeerflowChatTurnDTO])
async def list_session_deerflow_chat_turns(session_id: str) -> list[DeerflowChatTurnDTO]:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    return DEERFLOW_CHAT_TURNS.get(session_id, [])


@router.post("/{session_id}/deerflow/chat", response_model=DeerflowChatTurnDTO)
async def create_session_deerflow_chat_turn(session_id: str, body: SessionDeerflowChatRequest) -> DeerflowChatTurnDTO:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    scene = scene_cache_service.get_scene(session.scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="scene not found")
    if not session.deerflow_thread_id:
        raise HTTPException(status_code=400, detail="session thread not initialized")
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    result = bridge.invoke_analysis_skill(
        "chat",
        {
            "thread_id": session.deerflow_thread_id,
            "scene_id": session.scene_id,
            "scene_name": scene.name,
            "scene_version": session.scene_version,
            "message": message,
        },
    )
    turn = DeerflowChatTurnDTO(
        turn_id=f"turn_{uuid4().hex[:10]}",
        session_id=session_id,
        deerflow_thread_id=session.deerflow_thread_id,
        user_message=message,
        response_message=result.get("response", ""),
        mode=result.get("mode", "unknown"),
        success=bool(result.get("response")),
        error=result.get("error"),
        created_at=datetime.now(UTC),
    )
    DEERFLOW_CHAT_TURNS.setdefault(session_id, []).append(turn)
    persist_state()
    return turn


@router.post("/{session_id}/goal", response_model=AnalysisSessionDTO)
async def update_goal(session_id: str, body: UpdateGoalRequest) -> AnalysisSessionDTO:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    goal = body.global_goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="global_goal is required")
    session.global_goal = goal
    session.status = SessionStatus.PLANNING
    session.updated_at = session.created_at.__class__.now(session.updated_at.tzinfo)
    # Goal changed: invalidate stale plan/run so next execute must use fresh intent.
    QUERY_PLANS.pop(session_id, None)
    stale_query_ids = [query_id for query_id, run in QUERY_RUNS.items() if run.session_id == session_id]
    for query_id in stale_query_ids:
        QUERY_RUNS.pop(query_id, None)
    persist_state()
    return session.to_dto()


@router.post("/{session_id}/plan", response_model=dict)
async def generate_plan(session_id: str) -> dict:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    scene = scene_cache_service.get_scene(session.scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="scene not found")

    session.status = SessionStatus.WAITING_TASK_CONFIRMATION
    session.updated_at = session.created_at.__class__.now(session.updated_at.tzinfo)
    query_plan = build_query_plan(scene=scene, global_goal=session.global_goal, session_id=session_id)
    QUERY_PLANS[session_id] = query_plan
    persist_state()
    return {
        "session_id": session_id,
        "global_goal": session.global_goal,
        "suggested_tasks": [
            "确认分析口径与时间范围",
            "生成 Query Plan",
            "执行首轮 SQL 分析",
            "生成首张 slide draft",
        ],
        "query_plan_id": query_plan.query_plan_id,
        "status": session.status,
    }


@router.get("/{session_id}/current-query-plan", response_model=QueryPlanDTO)
async def get_current_query_plan(session_id: str) -> QueryPlanDTO:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    query_plan = QUERY_PLANS.get(session_id)
    if query_plan is None:
        scene = scene_cache_service.get_scene(session.scene_id)
        if scene is None:
            raise HTTPException(status_code=404, detail="scene not found")
        query_plan = build_query_plan(scene=scene, global_goal=session.global_goal, session_id=session_id)
        QUERY_PLANS[session_id] = query_plan
        session.status = SessionStatus.WAITING_QUERY_PLAN_CONFIRMATION
        session.updated_at = session.created_at.__class__.now(session.updated_at.tzinfo)
        persist_state()

    return query_plan


@router.post("/{session_id}/current-query/execute", response_model=QueryRunDTO)
async def execute_current_query(session_id: str) -> QueryRunDTO:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    scene = scene_cache_service.get_scene(session.scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="scene not found")

    result = sql_result_agent_service.run(
        scene=scene,
        session_id=session_id,
        scene_version=session.scene_version,
        intent=session.global_goal,
        agent_prompt="",
        context={"source": "analysis_sessions.current_query_execute"},
        execute=True,
    )
    query_plan = result.get("query_plan")
    query_run = result.get("query_run")
    if query_run is None:
        raise HTTPException(status_code=500, detail="query execution failed: no query run returned")

    if query_plan is not None:
        QUERY_PLANS[session_id] = query_plan
    QUERY_RUNS[query_run.query_id] = query_run
    try:
        runtime_persistence_service.save_query_result(
            session_id=session_id,
            query_plan=query_plan,
            query_run=query_run,
        )
    except Exception:  # noqa: BLE001
        # File-backed state remains the source of truth for the local demo.
        pass
    session.status = SessionStatus.SUMMARIZING_RESULT
    session.updated_at = session.created_at.__class__.now(session.updated_at.tzinfo)
    persist_state()
    return query_run


@router.get("/{session_id}/queries/{query_id}", response_model=QueryRunDTO)
async def get_query_run(session_id: str, query_id: str) -> QueryRunDTO:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    query_run = QUERY_RUNS.get(query_id)
    if query_run is None or query_run.session_id != session_id:
        raise HTTPException(status_code=404, detail="query run not found")
    return query_run
