from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.scene_cache_service import scene_cache_service
from ..services.runtime_persistence_service import runtime_persistence_service
from ..services.sql_result_agent_service import SqlResultAgentService
from ..store import QUERY_PLANS, QUERY_RUNS, SESSIONS, persist_state
from packages.analysis_domain.models import AnalysisSession
from packages.shared_contracts.python_models import QueryPlanDTO, QueryRunDTO, SessionStatus

router = APIRouter(prefix="/api/v1/sql-result-agent", tags=["sql-result-agent"])
service = SqlResultAgentService()


class GenerateSqlResultRequest(BaseModel):
    intent: str = ""
    agent_prompt: str = ""
    execute: bool = Field(default=True, description="whether execute sql after plan generation")
    context: dict | None = None


@router.get("/health")
async def sql_result_agent_health() -> dict:
    return service.health()


def _latest_query_run_for_session(session_id: str) -> QueryRunDTO | None:
    session_runs = [run for run in QUERY_RUNS.values() if run.session_id == session_id]
    if session_runs:
        return session_runs[-1]

    persisted = runtime_persistence_service.get_latest_query_result(session_id)
    if not persisted:
        return None

    query_plan_payload = persisted.get("query_plan") or None
    query_run_payload = persisted.get("query_run") or None
    if query_plan_payload:
        query_plan = QueryPlanDTO.model_validate(query_plan_payload)
        QUERY_PLANS[session_id] = query_plan
    if query_run_payload:
        query_run = QueryRunDTO.model_validate(query_run_payload)
        QUERY_RUNS[query_run.query_id] = query_run
        persist_state()
        return query_run
    return None


def _query_run_scene_id(query_run: QueryRunDTO | None) -> str:
    if query_run is None:
        return ""
    return str((query_run.lineage or {}).get("scene_id") or "").strip()


def _matches_scene(scene_id: str, session: AnalysisSession, query_run: QueryRunDTO | None) -> bool:
    scene_key = str(scene_id or "").strip()
    if not scene_key:
        return True
    run_scene_id = _query_run_scene_id(query_run)
    if run_scene_id:
        return run_scene_id == scene_key
    return session.scene_id == scene_key


def _sync_session_from_query_result(
    session: AnalysisSession,
    query_plan: QueryPlanDTO | None,
    query_run: QueryRunDTO | None,
) -> AnalysisSession:
    if query_run is not None:
        run_scene_id = _query_run_scene_id(query_run)
        if run_scene_id and session.scene_id != run_scene_id:
            session.scene_id = run_scene_id
        run_scene_version = str((query_run.lineage or {}).get("scene_version") or "").strip()
        if run_scene_version and session.scene_version != run_scene_version:
            session.scene_version = run_scene_version
    if query_plan is not None and query_plan.intent and session.global_goal != query_plan.intent:
        session.global_goal = query_plan.intent
    return session


def _recover_session_from_query_result(
    query_plan: QueryPlanDTO | None,
    query_run: QueryRunDTO,
    *,
    scene_hint: str = "",
) -> object:
    session = SESSIONS.get(query_run.session_id)
    if session is not None:
        return _sync_session_from_query_result(session, query_plan, query_run)

    scene_id = str(_query_run_scene_id(query_run) or scene_hint or "scene_prd_price").strip()
    scene_version = str((query_run.lineage or {}).get("scene_version") or "scene_v1").strip()
    goal = str((query_plan.intent if query_plan is not None else "") or query_run.sql_explanation or query_run.sql).strip()
    session = AnalysisSession(
        session_id=query_run.session_id,
        scene_id=scene_id,
        scene_version=scene_version,
        deerflow_thread_id=f"vibe_{query_run.session_id}",
        global_goal=goal or "已保存 SQL 分析",
        status=SessionStatus.SUMMARIZING_RESULT if query_run.status == "succeeded" else SessionStatus.FAILED,
    )
    SESSIONS[session.session_id] = session
    return session


@router.get("/history")
async def list_query_history(scene_id: str | None = None) -> dict:
    scene_key = str(scene_id or "").strip()
    sessions = sorted(SESSIONS.values(), key=lambda item: item.updated_at, reverse=True)
    entries = []
    seen_session_ids: set[str] = set()
    for session in sessions:
        query_run = _latest_query_run_for_session(session.session_id)
        if not _matches_scene(scene_key, session, query_run):
            continue
        query_plan = QUERY_PLANS.get(session.session_id)
        session = _sync_session_from_query_result(session, query_plan, query_run)
        seen_session_ids.add(session.session_id)
        entries.append(
            {
                "session": session.to_dto().model_dump(mode="json"),
                "query_plan": query_plan.model_dump(mode="json") if query_plan is not None else None,
                "query_run": query_run.model_dump(mode="json") if query_run is not None else None,
                "saved": query_run is not None,
            }
        )
    for persisted in runtime_persistence_service.list_query_results(limit=200):
        query_run_payload = persisted.get("query_run") or None
        if not query_run_payload:
            continue
        query_plan_payload = persisted.get("query_plan") or None
        query_plan = QueryPlanDTO.model_validate(query_plan_payload) if query_plan_payload else None
        query_run = QueryRunDTO.model_validate(query_run_payload)
        if query_run.session_id in seen_session_ids:
            continue
        run_scene_id = str((query_run.lineage or {}).get("scene_id") or "").strip()
        if scene_key and run_scene_id and run_scene_id != scene_key:
            continue
        existing_session = SESSIONS.get(query_run.session_id)
        if scene_key and not run_scene_id and existing_session is not None and existing_session.scene_id != scene_key:
            continue
        session = _recover_session_from_query_result(query_plan, query_run, scene_hint=scene_key)
        if not _matches_scene(scene_key, session, query_run):
            continue
        if query_plan is not None:
            QUERY_PLANS[session.session_id] = query_plan
        QUERY_RUNS[query_run.query_id] = query_run
        seen_session_ids.add(session.session_id)
        entries.append(
            {
                "session": session.to_dto().model_dump(mode="json"),
                "query_plan": query_plan.model_dump(mode="json") if query_plan is not None else None,
                "query_run": query_run.model_dump(mode="json"),
                "saved": True,
            }
        )
    if entries:
        persist_state()
    return {"ok": True, "items": entries}


@router.post("/sessions/{session_id}/generate-and-run")
async def generate_and_run_sql_result(session_id: str, body: GenerateSqlResultRequest) -> dict:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    scene = scene_cache_service.get_scene(session.scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    intent = body.intent.strip() or session.global_goal.strip()
    if not intent:
        raise HTTPException(status_code=400, detail="intent is required")

    session.global_goal = intent
    session.updated_at = datetime.now(UTC)
    result = service.run(
        scene=scene,
        session_id=session_id,
        scene_version=session.scene_version,
        intent=intent,
        agent_prompt=body.agent_prompt.strip(),
        context=body.context if isinstance(body.context, dict) else None,
        execute=body.execute,
    )

    query_plan = result.get("query_plan")
    query_run = result.get("query_run")
    if query_plan is not None:
        QUERY_PLANS[session_id] = query_plan
    if query_run is not None:
        QUERY_RUNS[query_run.query_id] = query_run
        try:
            mysql_save = runtime_persistence_service.save_query_result(
                session_id=session_id,
                query_plan=query_plan,
                query_run=query_run,
            )
        except Exception as exc:  # noqa: BLE001
            mysql_save = {"ok": False, "reason": str(exc)}
        session.status = SessionStatus.SUMMARIZING_RESULT if query_run.status == "succeeded" else SessionStatus.FAILED
        session.updated_at = datetime.now(UTC)
    persist_state()

    return {
        "ok": True,
        "session_id": session_id,
        "scene_id": scene.scene_id,
        "intent": intent,
        "provider": result.get("provider", "codex_cli"),
        "mode": result.get("mode", "local"),
        "notes": result.get("notes", []),
        "prompt_used": result.get("prompt_used", ""),
        "sql": result.get("sql", ""),
        "sql_explanation": result.get("sql_explanation", ""),
        "query_plan": query_plan.model_dump(mode="json") if query_plan is not None else None,
        "query_run": query_run.model_dump(mode="json") if query_run is not None else None,
        "saved": query_run is not None,
        "persistence": {
            "file_state": query_run is not None,
            "mysql": mysql_save if query_run is not None else None,
        },
    }


@router.get("/sessions/{session_id}/latest")
async def get_latest_sql_result(session_id: str) -> dict:
    session = SESSIONS.get(session_id)

    session_runs = [run for run in QUERY_RUNS.values() if run.session_id == session_id]
    if session_runs:
        query_run = session_runs[-1]
        query_plan = QUERY_PLANS.get(session_id)
        return {
            "ok": True,
            "session_id": session_id,
            "query_plan": query_plan.model_dump(mode="json") if query_plan is not None else None,
            "query_run": query_run.model_dump(mode="json"),
            "source": "memory",
        }

    persisted = runtime_persistence_service.get_latest_query_result(session_id)
    if persisted:
        query_plan_payload = persisted.get("query_plan") or None
        query_run_payload = persisted.get("query_run") or None
        query_plan = None
        if query_plan_payload:
            query_plan = QueryPlanDTO.model_validate(query_plan_payload)
            QUERY_PLANS[session_id] = query_plan
        if query_run_payload:
            query_run = QueryRunDTO.model_validate(query_run_payload)
            session = _recover_session_from_query_result(query_plan, query_run)
            QUERY_RUNS[query_run.query_id] = query_run
        persist_state()
        return {
            "ok": True,
            "session_id": session_id,
            "query_plan": query_plan_payload,
            "query_run": query_run_payload,
            "source": "mysql",
        }

    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    return {
        "ok": True,
        "session_id": session_id,
        "query_plan": None,
        "query_run": None,
        "source": "empty",
    }
