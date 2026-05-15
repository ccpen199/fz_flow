from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
from uuid import uuid4

from packages.shared_contracts.python_models import AnalysisSessionDTO, SessionStatus


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


@dataclass
class AnalysisSession:
    scene_id: str
    global_goal: str
    session_id: str = field(default_factory=lambda: make_id("sess"))
    scene_version: str | None = None
    deerflow_thread_id: str | None = None
    status: SessionStatus = SessionStatus.CREATED
    current_task_id: str | None = None
    deck_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_dto(cls, dto: AnalysisSessionDTO) -> "AnalysisSession":
        return cls(
            session_id=dto.session_id,
            scene_id=dto.scene_id,
            scene_version=dto.scene_version,
            deerflow_thread_id=dto.deerflow_thread_id,
            global_goal=dto.global_goal,
            status=dto.status,
            current_task_id=dto.current_task_id,
            deck_id=dto.deck_id,
            created_at=dto.created_at,
            updated_at=dto.updated_at,
        )

    def to_dto(self) -> AnalysisSessionDTO:
        return AnalysisSessionDTO(
            session_id=self.session_id,
            scene_id=self.scene_id,
            scene_version=self.scene_version,
            deerflow_thread_id=self.deerflow_thread_id,
            status=self.status,
            global_goal=self.global_goal,
            current_task_id=self.current_task_id,
            deck_id=self.deck_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )
