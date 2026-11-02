from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.routers import sql_result_agent
from app.store import DECK_REVISIONS, DECKS, PRESENTATION_TURNS, QUERY_PLANS, QUERY_RUNS, SESSIONS, SLIDE_DRAFTS, SLIDE_REVISIONS
from packages.analysis_domain.models import AnalysisSession
from packages.shared_contracts.python_models import QueryPlanDTO, QueryRunDTO, SessionStatus, SlideDraftDTO


def _reset() -> None:
    DECKS.clear()
    DECK_REVISIONS.clear()
    PRESENTATION_TURNS.clear()
    QUERY_PLANS.clear()
    QUERY_RUNS.clear()
    SESSIONS.clear()
    SLIDE_DRAFTS.clear()
    SLIDE_REVISIONS.clear()


def test_generate_and_run_sql_result_auto_writes_deck_and_resets_status(monkeypatch) -> None:
    _reset()
    session = AnalysisSession(
        session_id="sess_sql_router",
        scene_id="scene_sql_router",
        scene_version="v1",
        deerflow_thread_id="thread_sql_router",
        global_goal="销量分析",
        status=SessionStatus.GENERATING_SQL,
    )
    SESSIONS[session.session_id] = session

    scene = SimpleNamespace(scene_id=session.scene_id)
    query_plan = QueryPlanDTO(
        query_plan_id="plan_sql_router",
        session_id=session.session_id,
        intent=session.global_goal,
        metrics=[],
        dimensions=[],
        filters=[],
        chart_candidates=[],
        risk_notes=[],
    )
    query_run = QueryRunDTO(
        query_id="query_sql_router",
        session_id=session.session_id,
        query_plan_id=query_plan.query_plan_id,
        sql="SELECT 1",
        sql_explanation="测试查询",
        status="succeeded",
        rows_count=1,
        result_preview=[{"value": 1}],
        lineage={"scene_id": session.scene_id, "scene_version": session.scene_version},
    )
    slide = SlideDraftDTO(
        slide_id="slide_sql_router",
        session_id=session.session_id,
        query_id=query_run.query_id,
        page_type="overview",
        title="自动生成页面",
    )

    monkeypatch.setattr(sql_result_agent.scene_cache_service, "get_scene", lambda scene_id: scene if scene_id == scene.scene_id else None)
    monkeypatch.setattr(
        sql_result_agent.service,
        "run",
        lambda **kwargs: {
            "query_plan": query_plan,
            "query_run": query_run,
            "provider": "stub",
            "mode": "local",
            "notes": [],
        },
    )
    monkeypatch.setattr(sql_result_agent.runtime_persistence_service, "save_query_result", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(sql_result_agent, "build_slide_from_query", lambda **kwargs: slide)
    monkeypatch.setattr(sql_result_agent, "persist_state", lambda: None)

    result = asyncio.run(
        sql_result_agent.generate_and_run_sql_result(
            session.session_id,
            sql_result_agent.GenerateSqlResultRequest(intent=session.global_goal, execute=True, context={}),
        )
    )

    assert result["query_run"]["status"] == "succeeded"
    assert result["deck_error"] is None
    assert result["deck"]["deck_id"] == session.deck_id
    assert result["current_slide"]["slide_id"] == slide.slide_id
    assert result["deck_revision"]["deck_id"] == session.deck_id
    assert session.status == SessionStatus.SUMMARIZING_RESULT
    assert DECKS[session.deck_id].current_revision_id == result["deck_revision"]["deck_revision_id"]
