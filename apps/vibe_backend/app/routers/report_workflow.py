from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..services.presentation_workflow_service import presentation_workflow_service
from ..services.query_service import execute_raw_sql
from ..services.report_plan_service import report_plan_service
from ..services.report_pptx_export_service import export_report_pptx
from ..services.runtime_persistence_service import runtime_persistence_service
from ..services.scene_cache_service import scene_cache_service
from ..services.sql_result_agent_service import SqlResultAgentService
from ..store import DECKS, QUERY_PLANS, QUERY_RUNS, REPORT_PLANS, SESSIONS, SLIDE_DRAFTS, persist_state
from packages.shared_contracts.python_models import DeckDTO, QueryRunDTO, ReportPlanDTO, SessionStatus


router = APIRouter(prefix="/api/v1/analysis/sessions", tags=["report-workflow"])
sql_result_agent_service = SqlResultAgentService()


class GenerateReportPlanRequest(BaseModel):
    intent: str = Field(..., min_length=1)


class UpdateReportPageRequest(BaseModel):
    title: str | None = None
    table_header: str | None = None
    objective: str | None = None
    page_kind: str | None = None
    page_type: str | None = None
    metrics: list[str] | None = None
    dimensions: list[str] | None = None
    time_window: str | None = None
    filters: list[dict[str, Any]] | None = None
    chart_binding: dict[str, Any] | None = None


class UpdateSqlRequest(BaseModel):
    sql: str = Field(..., min_length=1)
    sql_explanation: str = ""


class ConfirmChartRequest(BaseModel):
    chart_binding: dict[str, Any] | None = None


class UpdateInsightRequest(BaseModel):
    findings: list[str] | None = None
    narrative: str | None = None


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _session_or_404(session_id: str):
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _plan_or_404(session_id: str, report_plan_id: str) -> ReportPlanDTO:
    plan = REPORT_PLANS.get(report_plan_id)
    if plan is None or plan.session_id != session_id:
        raise HTTPException(status_code=404, detail="report plan not found")
    return plan


def _scene_or_404(session):
    scene = scene_cache_service.get_scene(session.scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="scene not found")
    return scene


def _ensure_deck(session) -> DeckDTO:
    deck_id = session.deck_id or f"deck_{uuid4().hex[:10]}"
    session.deck_id = deck_id
    deck = DECKS.get(deck_id)
    if deck is None:
        deck = DeckDTO(
            deck_id=deck_id,
            session_id=session.session_id,
            title=f"Deck for {session.global_goal[:30]}",
            slide_ids=[],
            status="building",
        )
        DECKS[deck_id] = deck
    return deck


def _touch(plan: ReportPlanDTO, session) -> None:
    now = datetime.now(UTC)
    plan.updated_at = now
    session.updated_at = now
    REPORT_PLANS[plan.report_plan_id] = plan
    persist_state()


def _page_payload(plan: ReportPlanDTO, page_id: str) -> dict:
    try:
        page = report_plan_service.get_page(plan, page_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _dump(page)


@router.post("/{session_id}/report-plans", response_model=dict)
async def generate_report_plan(session_id: str, body: GenerateReportPlanRequest) -> dict:
    """Stage 1: produce an editable, complete PPT outline before SQL is requested."""
    session = _session_or_404(session_id)
    scene = _scene_or_404(session)
    intent = body.intent.strip()
    plan = report_plan_service.create_plan(session_id=session_id, scene=scene, intent=intent)
    REPORT_PLANS[plan.report_plan_id] = plan
    session.global_goal = intent
    session.status = SessionStatus.WAITING_TASK_CONFIRMATION
    _touch(plan, session)
    return {"ok": True, "report_plan": _dump(plan)}


@router.get("/{session_id}/report-plans", response_model=dict)
async def list_report_plans(session_id: str) -> dict:
    _session_or_404(session_id)
    plans = sorted(
        (plan for plan in REPORT_PLANS.values() if plan.session_id == session_id),
        key=lambda item: item.updated_at,
        reverse=True,
    )
    return {"ok": True, "items": [_dump(item) for item in plans]}


@router.get("/{session_id}/report-plans/{report_plan_id}", response_model=dict)
async def get_report_plan(session_id: str, report_plan_id: str) -> dict:
    return {"ok": True, "report_plan": _dump(_plan_or_404(session_id, report_plan_id))}


@router.post("/{session_id}/report-plans/{report_plan_id}/confirm-outline", response_model=dict)
async def confirm_report_outline(session_id: str, report_plan_id: str) -> dict:
    """HITL 1: do not make any SQL available until the outline has been confirmed."""
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    for page in plan.pages:
        if page.page_kind == "text" and page.page_type == "overview":
            page.stage = "insight_ready"
            page.narrative = page.objective
        else:
            page.stage = "sql_draft"
        page.updated_at = datetime.now(UTC)
    plan.status = "outline_confirmed"
    session.status = SessionStatus.GENERATING_SQL
    _touch(plan, session)
    return {"ok": True, "report_plan": _dump(plan)}


@router.post("/{session_id}/report-plans/{report_plan_id}/reopen-outline", response_model=dict)
async def reopen_report_outline(session_id: str, report_plan_id: str) -> dict:
    """Return to HITL 1 without creating a new session or losing prior page history."""
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    plan.status = "draft"
    for page in plan.pages:
        page.stage = "outline_draft"
        page.updated_at = datetime.now(UTC)
    session.status = SessionStatus.WAITING_TASK_CONFIRMATION
    _touch(plan, session)
    return {"ok": True, "report_plan": _dump(plan)}


@router.post("/{session_id}/report-plans/{report_plan_id}/pages", response_model=dict)
async def add_report_page(session_id: str, report_plan_id: str) -> dict:
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    page = report_plan_service.add_page(plan)
    _touch(plan, session)
    return {"ok": True, "page": _dump(page), "report_plan": _dump(plan)}


@router.patch("/{session_id}/report-plans/{report_plan_id}/pages/{page_id}", response_model=dict)
async def update_report_page(session_id: str, report_plan_id: str, page_id: str, body: UpdateReportPageRequest) -> dict:
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    try:
        page = report_plan_service.update_page(plan, page_id, body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _touch(plan, session)
    return {"ok": True, "page": _dump(page), "report_plan": _dump(plan)}


@router.delete("/{session_id}/report-plans/{report_plan_id}/pages/{page_id}", response_model=dict)
async def delete_report_page(session_id: str, report_plan_id: str, page_id: str) -> dict:
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    try:
        report_plan_service.delete_page(plan, page_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _touch(plan, session)
    return {"ok": True, "report_plan": _dump(plan)}


@router.post("/{session_id}/report-plans/{report_plan_id}/pages/{page_id}/activate", response_model=dict)
async def activate_report_page(session_id: str, report_plan_id: str, page_id: str) -> dict:
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    _page_payload(plan, page_id)
    plan.active_page_id = page_id
    _touch(plan, session)
    return {"ok": True, "report_plan": _dump(plan)}


@router.post("/{session_id}/report-plans/{report_plan_id}/pages/{page_id}/generate-sql", response_model=dict)
async def generate_page_sql(session_id: str, report_plan_id: str, page_id: str) -> dict:
    """Stage 2: generate SQL only for the human-confirmed page contract."""
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    if plan.status == "draft":
        raise HTTPException(status_code=409, detail="confirm the PPT outline before generating SQL")
    scene = _scene_or_404(session)
    try:
        page = report_plan_service.get_page(plan, page_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if page.page_kind == "text":
        raise HTTPException(status_code=400, detail="text page does not require a standalone SQL query")

    intent = report_plan_service.build_page_intent(plan, page)
    try:
        result = sql_result_agent_service.run(
            scene=scene,
            session_id=session_id,
            scene_version=session.scene_version,
            intent=intent,
            agent_prompt="",
            context={
                "source": "report_workflow.page_sql",
                "report_plan_id": plan.report_plan_id,
                "report_page_id": page.page_id,
                "chart_binding": page.chart_binding.model_dump(mode="json"),
                "planned_metrics": page.metrics,
                "planned_dimensions": page.dimensions,
            },
            execute=False,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"SQL Agent 调用失败：{exc}") from exc

    query_plan = result.get("query_plan")
    sql = str(result.get("sql") or "").strip()
    if query_plan is not None:
        query_plan.task_id = page.page_id
        QUERY_PLANS[query_plan.query_plan_id] = query_plan
    query_run = QueryRunDTO(
        query_id=f"query_{uuid4().hex[:10]}",
        session_id=session_id,
        task_id=page.page_id,
        query_plan_id=query_plan.query_plan_id if query_plan is not None else None,
        sql=sql,
        sql_explanation=str(result.get("sql_explanation") or "").strip(),
        status="generated",
        chart_suggestion=page.chart_binding.chart_type,
        lineage={
            "report_plan_id": plan.report_plan_id,
            "report_page_id": page.page_id,
            "scene_id": scene.scene_id,
            "scene_version": session.scene_version,
        },
    )
    QUERY_RUNS[query_run.query_id] = query_run
    page.query_plan_id = query_run.query_plan_id
    page.query_id = query_run.query_id
    page.sql = sql
    page.sql_explanation = query_run.sql_explanation
    page.stage = "sql_ready"
    page.updated_at = datetime.now(UTC)
    session.status = SessionStatus.WAITING_SQL_CONFIRMATION
    _touch(plan, session)
    return {"ok": True, "page": _dump(page), "query_plan": _dump(query_plan), "query_run": _dump(query_run)}


@router.put("/{session_id}/report-plans/{report_plan_id}/pages/{page_id}/sql", response_model=dict)
async def update_page_sql(session_id: str, report_plan_id: str, page_id: str, body: UpdateSqlRequest) -> dict:
    """HITL 2: save a reviewed SQL statement; execution remains a separate action."""
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    try:
        page = report_plan_service.get_page(plan, page_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    page.sql = body.sql.strip()
    page.sql_explanation = body.sql_explanation.strip()
    page.stage = "sql_ready"
    page.updated_at = datetime.now(UTC)
    if page.query_id and page.query_id in QUERY_RUNS:
        QUERY_RUNS[page.query_id].sql = page.sql
        QUERY_RUNS[page.query_id].sql_explanation = page.sql_explanation
    _touch(plan, session)
    return {"ok": True, "page": _dump(page)}


@router.post("/{session_id}/report-plans/{report_plan_id}/pages/{page_id}/execute", response_model=dict)
async def execute_page_sql(session_id: str, report_plan_id: str, page_id: str) -> dict:
    """HITL 2 confirmation: execute exactly the saved, read-only SQL for this page."""
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    scene = _scene_or_404(session)
    try:
        page = report_plan_service.get_page(plan, page_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not page.sql.strip():
        raise HTTPException(status_code=400, detail="generate or save SQL before execution")
    session.status = SessionStatus.EXECUTING_QUERY
    query_run = execute_raw_sql(
        session_id=session_id,
        scene=scene,
        scene_version=session.scene_version,
        sql=page.sql,
        query_plan_id=page.query_plan_id,
        sql_explanation=page.sql_explanation,
        lineage_extra={"report_plan_id": plan.report_plan_id, "report_page_id": page.page_id},
    )
    query_run.task_id = page.page_id
    QUERY_RUNS[query_run.query_id] = query_run
    page.query_id = query_run.query_id
    page.sql = query_run.sql
    page.sql_explanation = query_run.sql_explanation
    page.result_preview = query_run.result_preview
    page.rows_count = query_run.rows_count
    page.stage = "data_ready" if query_run.status == "succeeded" else "sql_ready"
    page.updated_at = datetime.now(UTC)
    if page.query_plan_id and page.query_plan_id in QUERY_PLANS:
        try:
            runtime_persistence_service.save_query_result(
                session_id=session_id,
                query_plan=QUERY_PLANS[page.query_plan_id],
                query_run=query_run,
            )
        except Exception:  # Persistence must not hide a valid query result.
            pass
    session.status = SessionStatus.WAITING_SLIDE_REVIEW if query_run.status == "succeeded" else SessionStatus.WAITING_SQL_CONFIRMATION
    _touch(plan, session)
    return {"ok": query_run.status == "succeeded", "page": _dump(page), "query_run": _dump(query_run)}


@router.post("/{session_id}/report-plans/{report_plan_id}/pages/{page_id}/confirm-chart", response_model=dict)
async def confirm_page_chart(session_id: str, report_plan_id: str, page_id: str, body: ConfirmChartRequest) -> dict:
    """HITL 3: make the selected chart type and exact data fields explicit."""
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    try:
        page = report_plan_service.get_page(plan, page_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if page.stage not in {"data_ready", "chart_ready", "insight_ready", "approved"}:
        raise HTTPException(status_code=409, detail="execute the page query before confirming its chart")
    if body.chart_binding:
        page = report_plan_service.update_page(plan, page_id, {"chart_binding": body.chart_binding})
    page.stage = "chart_ready"
    page.updated_at = datetime.now(UTC)
    _touch(plan, session)
    return {"ok": True, "page": _dump(page)}


@router.post("/{session_id}/report-plans/{report_plan_id}/pages/{page_id}/generate-insight", response_model=dict)
async def generate_page_insight(session_id: str, report_plan_id: str, page_id: str) -> dict:
    """Stage 4: calculate narrative from this page's actual confirmed dataset."""
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    try:
        page = report_plan_service.get_page(plan, page_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if page.page_kind != "text" and page.stage not in {"chart_ready", "insight_ready", "approved"}:
        raise HTTPException(status_code=409, detail="confirm chart and data before generating insight")
    page.findings, page.narrative = report_plan_service.generate_insight(plan, page)
    page.stage = "insight_ready"
    page.updated_at = datetime.now(UTC)
    _touch(plan, session)
    return {"ok": True, "page": _dump(page)}


@router.put("/{session_id}/report-plans/{report_plan_id}/pages/{page_id}/insight", response_model=dict)
async def update_page_insight(session_id: str, report_plan_id: str, page_id: str, body: UpdateInsightRequest) -> dict:
    """HITL 4: save human-edited page conclusions without regenerating data."""
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    try:
        page = report_plan_service.get_page(plan, page_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if body.findings is not None:
        page.findings = [str(item).strip() for item in body.findings if str(item).strip()]
    if body.narrative is not None:
        page.narrative = body.narrative.strip()
    page.stage = "insight_ready"
    page.updated_at = datetime.now(UTC)
    _touch(plan, session)
    return {"ok": True, "page": _dump(page)}


@router.post("/{session_id}/report-plans/{report_plan_id}/pages/{page_id}/approve", response_model=dict)
async def approve_report_page(session_id: str, report_plan_id: str, page_id: str) -> dict:
    """Confirmation writes directly into the session Deck; there is no second app or 'import Deck' step."""
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    try:
        page = report_plan_service.get_page(plan, page_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if page.stage not in {"insight_ready", "approved"}:
        raise HTTPException(status_code=409, detail="generate and review the page insight before confirming the page")
    deck = _ensure_deck(session)
    next_slide = report_plan_service.to_slide(session_id=session_id, page=page)
    if page.slide_id and page.slide_id in deck.slide_ids:
        changed, revision = presentation_workflow_service.apply_revision(
            deck=deck,
            target_slide_ids=[page.slide_id],
            patch={
                "title": next_slide.title,
                "subtitle": next_slide.subtitle,
                "chart_spec": next_slide.chart_spec,
                "findings": next_slide.findings,
                "narrative": next_slide.narrative,
                "recommendations": [],
            },
            turn_id=None,
            change_summary=f"确认报告第{page.order}页：{page.title}",
        )
        slide = changed[0]
    else:
        slide = next_slide
        revision = presentation_workflow_service.append_slide(
            deck=deck,
            slide=slide,
            turn_id=None,
            change_summary=f"确认报告第{page.order}页：{page.title}",
        )
    page.slide_id = slide.slide_id
    page.stage = "approved"
    page.updated_at = datetime.now(UTC)
    SLIDE_DRAFTS[slide.slide_id] = slide
    SLIDE_DRAFTS[session_id] = slide
    ordered_page_slide_ids = [
        item.slide_id
        for item in sorted(plan.pages, key=lambda item: item.order)
        if item.slide_id and item.slide_id in deck.slide_ids
    ]
    if ordered_page_slide_ids and deck.slide_ids != ordered_page_slide_ids:
        deck.slide_ids = ordered_page_slide_ids
        revision = presentation_workflow_service.record_deck_revision(
            deck=deck,
            turn_id=None,
            change_summary="按 PPT 大纲顺序同步页面",
        )
    DECKS[deck.deck_id] = deck
    plan.status = "ready_to_export" if all(item.stage == "approved" for item in plan.pages) else "in_progress"
    session.status = SessionStatus.COMPLETED if plan.status == "ready_to_export" else SessionStatus.UPDATING_DECK
    _touch(plan, session)
    return {"ok": True, "page": _dump(page), "slide": _dump(slide), "deck": _dump(deck), "deck_revision": _dump(revision)}


@router.get("/{session_id}/report-plans/{report_plan_id}/export.pptx", response_class=FileResponse)
async def export_report_plan_pptx(session_id: str, report_plan_id: str):
    """Stage 5: export the confirmed in-app Deck as an editable PPTX file."""
    session = _session_or_404(session_id)
    plan = _plan_or_404(session_id, report_plan_id)
    if not plan.pages or not all(page.stage == "approved" for page in plan.pages):
        raise HTTPException(status_code=409, detail="confirm every report page before exporting the complete PPT")
    deck = _ensure_deck(session)
    slides = [
        SLIDE_DRAFTS[page.slide_id]
        for page in sorted(plan.pages, key=lambda item: item.order)
        if page.slide_id and page.slide_id in SLIDE_DRAFTS
    ]
    if len(slides) != len(plan.pages):
        raise HTTPException(status_code=409, detail="some confirmed pages are not yet available in the Deck")
    path = export_report_pptx(deck=deck, slides=slides)
    plan.status = "exported"
    _touch(plan, session)
    safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in plan.title)[:64] or deck.deck_id
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"{safe_name}.pptx",
    )
