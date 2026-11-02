from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    WAITING_TASK_CONFIRMATION = "waiting_task_confirmation"
    GENERATING_QUERY_PLAN = "generating_query_plan"
    WAITING_QUERY_PLAN_CONFIRMATION = "waiting_query_plan_confirmation"
    GENERATING_SQL = "generating_sql"
    WAITING_SQL_CONFIRMATION = "waiting_sql_confirmation"
    EXECUTING_QUERY = "executing_query"
    SUMMARIZING_RESULT = "summarizing_result"
    GENERATING_SLIDE_DRAFT = "generating_slide_draft"
    WAITING_SLIDE_REVIEW = "waiting_slide_review"
    UPDATING_DECK = "updating_deck"
    RECOMMENDING_NEXT_TASK = "recommending_next_task"
    WAITING_NEXT_TASK_CONFIRMATION = "waiting_next_task_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FieldRole(str, Enum):
    METRIC = "metric"
    DIMENSION = "dimension"
    TIME = "time"
    FILTER = "filter"


class SceneFieldDTO(BaseModel):
    field_id: str
    table_name: str
    field_name: str
    semantic_name: str
    description: str
    role: FieldRole
    enabled: bool = True


class SceneRelationDTO(BaseModel):
    relation_id: str
    left_table: str
    left_field: str
    right_table: str
    right_field: str
    join_type: str = "INNER"
    note: str = ""


class SceneDTO(BaseModel):
    scene_id: str
    name: str
    description: str
    version: int = 1
    sample_goals: list[str] = Field(default_factory=list)
    fields: list[SceneFieldDTO] = Field(default_factory=list)
    relations: list[SceneRelationDTO] = Field(default_factory=list)


class AnalysisSessionDTO(BaseModel):
    session_id: str
    user_id: str | None = None
    scene_id: str
    scene_version: str | None = None
    deerflow_thread_id: str | None = None
    status: SessionStatus
    global_goal: str
    current_task_id: str | None = None
    deck_id: str | None = None
    created_at: datetime
    updated_at: datetime


class QueryPlanDTO(BaseModel):
    query_plan_id: str
    session_id: str
    task_id: str | None = None
    intent: str
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    time_window: str | None = None
    chart_candidates: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class QueryRunDTO(BaseModel):
    query_id: str
    session_id: str
    task_id: str | None = None
    query_plan_id: str | None = None
    sql: str
    sql_explanation: str = ""
    status: Literal["generated", "approved", "running", "succeeded", "failed", "cancelled"] = "generated"
    rows_count: int = 0
    duration_ms: int | None = None
    result_preview: list[dict[str, Any]] = Field(default_factory=list)
    insight_summary: list[str] = Field(default_factory=list)
    chart_suggestion: str | None = None
    safety_checks: list[dict[str, Any]] = Field(default_factory=list)
    lineage: dict[str, Any] = Field(default_factory=dict)


# The chart vocabulary intentionally mirrors TrainPPTAgent's AIPPTChartType.
# Keeping it in the shared contract lets the FZ workbench plan a page, query its
# data, render it, and finally hand the very same document to a PPT exporter.
ReportChartType = Literal["bar", "column", "line", "area", "pie", "ring", "radar", "scatter", "table", "none"]
ReportPageKind = Literal["text", "chart", "table"]
ReportPageStage = Literal[
    "outline_draft",
    "sql_draft",
    "sql_ready",
    "data_ready",
    "chart_ready",
    "insight_ready",
    "approved",
]
ReportPlanStatus = Literal["draft", "outline_confirmed", "in_progress", "ready_to_export", "exported"]


class ReportChartBindingDTO(BaseModel):
    """The explicit data-to-chart contract for one planned report page."""

    chart_type: ReportChartType = "bar"
    label_field: str = ""
    value_fields: list[str] = Field(default_factory=list)
    series_field: str = ""
    max_rows: int = 12
    filters: list[dict[str, Any]] = Field(default_factory=list)


class ReportPageDTO(BaseModel):
    """A human-reviewable page plan and its later query/chart/insight artifacts."""

    page_id: str
    order: int
    page_kind: ReportPageKind = "chart"
    page_type: Literal["overview", "comparison", "trend", "root_cause", "risk", "summary"] = "overview"
    title: str
    table_header: str = ""
    objective: str = ""
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    time_window: str = ""
    filters: list[dict[str, Any]] = Field(default_factory=list)
    chart_binding: ReportChartBindingDTO = Field(default_factory=ReportChartBindingDTO)
    stage: ReportPageStage = "outline_draft"
    query_plan_id: str | None = None
    query_id: str | None = None
    sql: str = ""
    sql_explanation: str = ""
    result_preview: list[dict[str, Any]] = Field(default_factory=list)
    rows_count: int = 0
    findings: list[str] = Field(default_factory=list)
    narrative: str = ""
    slide_id: str | None = None
    updated_at: datetime


class ReportPlanDTO(BaseModel):
    """A persistent, multi-page report conversation; never a one-query scratchpad."""

    report_plan_id: str
    session_id: str
    title: str
    intent: str
    analysis_scope: dict[str, Any] = Field(default_factory=dict)
    status: ReportPlanStatus = "draft"
    pages: list[ReportPageDTO] = Field(default_factory=list)
    active_page_id: str | None = None
    version: int = 1
    created_at: datetime
    updated_at: datetime


class PreferenceDTO(BaseModel):
    user_id: str = "default_user"
    ppt_tone: str = "professional"
    ppt_template: str = "corporate_default"
    preferred_chart_types: list[str] = Field(default_factory=lambda: ["bar", "line"])
    default_language: str = "zh-CN"


class MemoryEntryDTO(BaseModel):
    memory_id: str
    user_id: str = "default_user"
    topic: str
    content: str
    tags: list[str] = Field(default_factory=list)


class SlideDraftDTO(BaseModel):
    slide_id: str
    session_id: str
    task_id: str | None = None
    query_id: str | None = None
    page_type: Literal["overview", "comparison", "trend", "root_cause", "risk", "summary"]
    title: str
    subtitle: str = ""
    chart_spec: dict[str, Any] = Field(default_factory=dict)
    findings: list[str] = Field(default_factory=list)
    narrative: str = ""
    recommendations: list[str] = Field(default_factory=list)
    lineage_summary: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending_review"
    version: int = 1


class DeckDTO(BaseModel):
    deck_id: str
    session_id: str
    title: str
    slide_ids: list[str] = Field(default_factory=list)
    status: str = "building"
    current_revision_id: str | None = None
    style_tokens: dict[str, Any] = Field(default_factory=dict)
    editor_document: dict[str, Any] | None = None


class PresentationTurnDTO(BaseModel):
    turn_id: str
    session_id: str
    kind: Literal["analysis", "presentation_revision", "rollback"]
    user_message: str
    status: Literal["planned", "applied", "needs_confirmation", "failed"] = "planned"
    response_message: str = ""
    query_id: str | None = None
    deck_id: str | None = None
    deck_revision_id: str | None = None
    target_slide_ids: list[str] = Field(default_factory=list)
    planner_mode: str = "rules"
    action_plan: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime


class SlideRevisionDTO(BaseModel):
    slide_revision_id: str
    slide_id: str
    deck_id: str | None = None
    session_id: str
    parent_slide_revision_id: str | None = None
    turn_id: str | None = None
    version: int = 1
    slide: SlideDraftDTO
    change_summary: str = ""
    created_at: datetime


class DeckRevisionDTO(BaseModel):
    deck_revision_id: str
    deck_id: str
    session_id: str
    parent_deck_revision_id: str | None = None
    turn_id: str | None = None
    version: int = 1
    title: str = ""
    slide_ids: list[str] = Field(default_factory=list)
    current_slide_id: str | None = None
    style_tokens: dict[str, Any] = Field(default_factory=dict)
    editor_document: dict[str, Any] | None = None
    change_summary: str = ""
    created_at: datetime


class DeerflowChatTurnDTO(BaseModel):
    turn_id: str
    session_id: str
    deerflow_thread_id: str
    user_message: str
    response_message: str = ""
    mode: str = "stub"
    success: bool = False
    error: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Conversational canvas contracts (M1: chat stream + evolving canvas)
#
# The canvas is a list of addressable ArtifactCards. A card is the only thing
# the frontend renderer dispatches on, so new card kinds never change the main
# chat/canvas loop. Every card carries lineage back to the message, the query
# and the SQL that produced it, which keeps the whole conversation auditable.
# ---------------------------------------------------------------------------

MessageRole = Literal["user", "assistant", "system"]
MessageStatus = Literal["done", "failed"]
ArtifactKind = Literal[
    "dimension_card",
    "chart_card",
    # Cards that block the flow until a human settles something. They exist so
    # the conversation keeps the config UI's two mandatory human checkpoints:
    # field-value disambiguation and statistical-caliber clarification.
    "resolution_card",
    "clarification_card",
    "outline_card",
    "deck_card",
    "note_card",
]


class WorkingMemoryDTO(BaseModel):
    """Cross-turn context so the agent keeps track of what has already happened."""

    brand: str = ""
    selected_dimension_keys: list[str] = Field(default_factory=list)
    produced_artifact_ids: list[str] = Field(default_factory=list)
    chart_artifact_ids: list[str] = Field(default_factory=list)
    active_outline_id: str | None = None
    active_deck_id: str | None = None
    last_intent: str = ""
    scene_locked_by: str = "auto"
    # Values a human has explicitly pinned on the canvas (was "字段消歧确认").
    # These are handed to the SQL safety gates so a settled tie stays settled.
    confirmed_field_values: list[dict[str, Any]] = Field(default_factory=list)
    # Statistical calibers the user confirmed per dimension, e.g.
    # {"fiber": "按 SKU 加权后再汇总"}. Re-applied on every later re-run.
    clarifications: dict[str, str] = Field(default_factory=dict)
    # The question the canvas is currently waiting on, if any.
    pending_blocker: dict[str, Any] | None = None
    # 最近一轮**实际加载**了哪几套配置（`config_ids` / `names` / `merged`）。
    #
    # 配置已经从"会话属性"改成"按需加载的轮次资源"：每一轮由多轮 agent 按问题判定
    # 需要哪几套，后端按需加载，不涉及配置的轮次一个都不导入。所以这里记的是
    # **本轮的加载结果**，会随轮次变化——不再是"这个会话用哪个场景"。
    # 空字典 = 最近一轮没有加载任何配置（闲聊、改画法、页码操作等），这是正常状态。
    turn_config: dict[str, Any] = Field(default_factory=dict)


class MessageDTO(BaseModel):
    """One turn in the left-hand chat stream."""

    message_id: str
    conversation_id: str
    role: MessageRole
    content: str
    intent: str = ""
    artifact_refs: list[str] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    # 这一轮**实际加载**的配置（config_ids / names / note…），来自
    # ``working_memory.turn_config``。挂在消息上而不是只在会话里留一份，
    # 是为了让对话流能逐轮回溯"这句用了哪套配置"——头部胶囊只反映最后一轮。
    turn_config: dict[str, Any] = Field(default_factory=dict)
    status: MessageStatus = "done"
    error: str | None = None
    created_at: datetime


class ArtifactCardDTO(BaseModel):
    """An addressable card on the right-hand canvas."""

    artifact_id: str
    conversation_id: str
    message_id: str | None = None
    kind: ArtifactKind
    title: str
    subtitle: str = ""
    status: str = "ready"
    payload: dict[str, Any] = Field(default_factory=dict)
    lineage: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    history: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ConversationDTO(BaseModel):
    """A conversational analysis workspace bound to a scene and a canvas."""

    conversation_id: str
    title: str = ""
    scene_id: str | None = None
    scene_name: str = ""
    status: Literal["active", "archived"] = "active"
    working_memory: WorkingMemoryDTO = Field(default_factory=WorkingMemoryDTO)
    created_at: datetime
    updated_at: datetime
