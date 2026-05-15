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


class ArtifactDTO(BaseModel):
    artifact_id: str
    artifact_type: str
    file_name: str
    download_url: str
    local_path: str | None = None
    session_id: str | None = None
    deck_id: str | None = None


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
