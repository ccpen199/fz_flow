from __future__ import annotations

import json
import os
from pathlib import Path

from packages.analysis_domain.models import AnalysisSession
from packages.shared_contracts.python_models import (
    AnalysisSessionDTO,
    ArtifactCardDTO,
    ConversationDTO,
    DeckDTO,
    DeckRevisionDTO,
    DeerflowChatTurnDTO,
    MemoryEntryDTO,
    MessageDTO,
    PresentationTurnDTO,
    PreferenceDTO,
    QueryPlanDTO,
    QueryRunDTO,
    ReportPlanDTO,
    SceneDTO,
    SlideDraftDTO,
    SlideRevisionDTO,
)


SCENES: dict[str, SceneDTO] = {}
SESSIONS: dict[str, AnalysisSession] = {}
QUERY_PLANS: dict[str, QueryPlanDTO] = {}
QUERY_RUNS: dict[str, QueryRunDTO] = {}
REPORT_PLANS: dict[str, ReportPlanDTO] = {}
SLIDE_DRAFTS: dict[str, SlideDraftDTO] = {}
DECKS: dict[str, DeckDTO] = {}
PRESENTATION_TURNS: dict[str, list[PresentationTurnDTO]] = {}
SLIDE_REVISIONS: dict[str, SlideRevisionDTO] = {}
DECK_REVISIONS: dict[str, DeckRevisionDTO] = {}
CONVERSATIONS: dict[str, ConversationDTO] = {}
MESSAGES: dict[str, list[MessageDTO]] = {}
ARTIFACTS: dict[str, ArtifactCardDTO] = {}
PREFERENCES: dict[str, PreferenceDTO] = {
    "default_user": PreferenceDTO(),
}
MEMORY_ENTRIES: dict[str, MemoryEntryDTO] = {}
DEERFLOW_CHAT_TURNS: dict[str, list[DeerflowChatTurnDTO]] = {}

STATE_ROOT = Path("runtime_data/backend_state")
STATE_FILE = STATE_ROOT / "state.json"
FILE_STATE_ENABLED = os.getenv("VIBE_PERSIST_STATE", "0") == "1"
MYSQL_STATE_ENABLED = os.getenv("VIBE_MYSQL_PERSIST_STATE", "1") != "0"


def _dump_models(data: dict) -> dict:
    return {
        key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        for key, value in data.items()
    }


def _state_payload() -> dict:
    return {
        "scenes": _dump_models(SCENES),
        "sessions": {key: value.to_dto().model_dump(mode="json") for key, value in SESSIONS.items()},
        "query_plans": _dump_models(QUERY_PLANS),
        "query_runs": _dump_models(QUERY_RUNS),
        "report_plans": _dump_models(REPORT_PLANS),
        "slide_drafts": _dump_models(SLIDE_DRAFTS),
        "decks": _dump_models(DECKS),
        "presentation_turns": {
            key: [item.model_dump(mode="json") for item in value]
            for key, value in PRESENTATION_TURNS.items()
        },
        "slide_revisions": _dump_models(SLIDE_REVISIONS),
        "deck_revisions": _dump_models(DECK_REVISIONS),
        "preferences": _dump_models(PREFERENCES),
        "memory_entries": _dump_models(MEMORY_ENTRIES),
        "deerflow_chat_turns": {key: [item.model_dump(mode="json") for item in value] for key, value in DEERFLOW_CHAT_TURNS.items()},
        "conversations": _dump_models(CONVERSATIONS),
        "messages": {key: [item.model_dump(mode="json") for item in value] for key, value in MESSAGES.items()},
        "artifacts": _dump_models(ARTIFACTS),
    }


def _persist_state_to_mysql(payload: dict) -> None:
    try:
        from .services.runtime_persistence_service import runtime_persistence_service

        runtime_persistence_service.save_workflow_state(payload)
        for session_id, session_payload in payload.get("sessions", {}).items():
            runtime_persistence_service.save_workflow_stage(
                session_id=session_id,
                stage_key="session",
                payload=session_payload,
            )
        for plan_key, plan_payload in payload.get("query_plans", {}).items():
            session_id = str(plan_payload.get("session_id") or "").strip()
            if not session_id:
                continue
            plan_id = str(plan_payload.get("query_plan_id") or plan_key).strip()
            runtime_persistence_service.save_workflow_stage(
                session_id=session_id,
                stage_key=f"query_plan:{plan_id}",
                payload=plan_payload,
            )
            if plan_key == session_id:
                runtime_persistence_service.save_workflow_stage(
                    session_id=session_id,
                    stage_key="query_plan",
                    payload=plan_payload,
                )
        for query_id, run_payload in payload.get("query_runs", {}).items():
            session_id = str(run_payload.get("session_id") or "").strip()
            if not session_id:
                continue
            runtime_persistence_service.save_workflow_stage(
                session_id=session_id,
                stage_key=f"query_run:{query_id}",
                payload=run_payload,
            )
        for report_plan_id, report_plan_payload in payload.get("report_plans", {}).items():
            session_id = str(report_plan_payload.get("session_id") or "").strip()
            if session_id:
                runtime_persistence_service.save_workflow_stage(
                    session_id=session_id,
                    stage_key=f"report_plan:{report_plan_id}",
                    payload=report_plan_payload,
                )
        for slide_id, slide_payload in payload.get("slide_drafts", {}).items():
            session_id = str(slide_payload.get("session_id") or "").strip()
            if not session_id or slide_id == session_id:
                continue
            runtime_persistence_service.save_workflow_stage(
                session_id=session_id,
                stage_key=f"slide:{slide_id}",
                payload=slide_payload,
            )
        for deck_id, deck_payload in payload.get("decks", {}).items():
            session_id = str(deck_payload.get("session_id") or "").strip()
            if not session_id:
                continue
            runtime_persistence_service.save_workflow_stage(
                session_id=session_id,
                stage_key=f"deck:{deck_id}",
                payload=deck_payload,
            )
        for session_id, turns in payload.get("presentation_turns", {}).items():
            for turn_payload in turns:
                turn_id = str(turn_payload.get("turn_id") or "").strip()
                if turn_id:
                    runtime_persistence_service.save_workflow_stage(
                        session_id=session_id,
                        stage_key=f"presentation_turn:{turn_id}",
                        payload=turn_payload,
                    )
        for revision_id, revision_payload in payload.get("slide_revisions", {}).items():
            session_id = str(revision_payload.get("session_id") or "").strip()
            if session_id:
                runtime_persistence_service.save_workflow_stage(
                    session_id=session_id,
                    stage_key=f"slide_revision:{revision_id}",
                    payload=revision_payload,
                )
        for revision_id, revision_payload in payload.get("deck_revisions", {}).items():
            session_id = str(revision_payload.get("session_id") or "").strip()
            if session_id:
                runtime_persistence_service.save_workflow_stage(
                    session_id=session_id,
                    stage_key=f"deck_revision:{revision_id}",
                    payload=revision_payload,
                )
    except Exception as exc:  # noqa: BLE001
        print(f"[store] mysql workflow persistence skipped: {exc}")


def persist_state() -> None:
    payload = _state_payload()
    if FILE_STATE_ENABLED:
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if MYSQL_STATE_ENABLED:
        _persist_state_to_mysql(payload)


def _migrate_removed_presenton_metadata(value: object) -> object:
    """Keep saved slide/deck data readable after the Presenton export path is removed."""
    if isinstance(value, list):
        return [_migrate_removed_presenton_metadata(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _migrate_removed_presenton_metadata(item)
            for key, item in value.items()
            if key not in {"ppt_scheme", "ppt_scheme_name"}
        }
    if not isinstance(value, str):
        return value
    return (
        value.replace("Presenton AI 导出", "PPT 页面草稿")
        .replace("Presenton AI PPT 生成", "TrainPPTAgent 页面编辑")
        .replace("作为 Presenton 大模型生成 PPT 的结构化输入", "作为供 TrainPPTAgent 编辑的结构化页面草稿")
        .replace("导出阶段会把结论、数据预览和生成指令发送给 Presenton。", "写入 Deck 后可进入 TrainPPTAgent 继续编辑。")
        .replace("批准入 Deck 后调用 Presenton 生成 PPTX", "写入 Deck 后在 TrainPPTAgent 中编辑")
    )


def _load_payload(payload: dict) -> None:
    payload = _migrate_removed_presenton_metadata(payload)
    if not isinstance(payload, dict):
        raise ValueError("invalid workflow state payload")
    SCENES.clear()
    SCENES.update({key: SceneDTO.model_validate(value) for key, value in payload.get("scenes", {}).items()})

    SESSIONS.clear()
    SESSIONS.update(
        {
            key: AnalysisSession.from_dto(AnalysisSessionDTO.model_validate(value))
            for key, value in payload.get("sessions", {}).items()
        }
    )

    QUERY_PLANS.clear()
    QUERY_PLANS.update(
        {key: QueryPlanDTO.model_validate(value) for key, value in payload.get("query_plans", {}).items()}
    )

    QUERY_RUNS.clear()
    QUERY_RUNS.update(
        {key: QueryRunDTO.model_validate(value) for key, value in payload.get("query_runs", {}).items()}
    )

    REPORT_PLANS.clear()
    REPORT_PLANS.update(
        {key: ReportPlanDTO.model_validate(value) for key, value in payload.get("report_plans", {}).items()}
    )

    SLIDE_DRAFTS.clear()
    SLIDE_DRAFTS.update(
        {key: SlideDraftDTO.model_validate(value) for key, value in payload.get("slide_drafts", {}).items()}
    )

    DECKS.clear()
    DECKS.update({key: DeckDTO.model_validate(value) for key, value in payload.get("decks", {}).items()})

    PRESENTATION_TURNS.clear()
    PRESENTATION_TURNS.update(
        {
            key: [PresentationTurnDTO.model_validate(item) for item in value]
            for key, value in payload.get("presentation_turns", {}).items()
        }
    )

    SLIDE_REVISIONS.clear()
    SLIDE_REVISIONS.update(
        {
            key: SlideRevisionDTO.model_validate(value)
            for key, value in payload.get("slide_revisions", {}).items()
        }
    )

    DECK_REVISIONS.clear()
    DECK_REVISIONS.update(
        {
            key: DeckRevisionDTO.model_validate(value)
            for key, value in payload.get("deck_revisions", {}).items()
        }
    )


    PREFERENCES.clear()
    PREFERENCES.update(
        {key: PreferenceDTO.model_validate(value) for key, value in payload.get("preferences", {}).items()}
    )
    if "default_user" not in PREFERENCES:
        PREFERENCES["default_user"] = PreferenceDTO()

    MEMORY_ENTRIES.clear()
    MEMORY_ENTRIES.update(
        {key: MemoryEntryDTO.model_validate(value) for key, value in payload.get("memory_entries", {}).items()}
    )

    DEERFLOW_CHAT_TURNS.clear()
    DEERFLOW_CHAT_TURNS.update(
        {
            key: [DeerflowChatTurnDTO.model_validate(item) for item in value]
            for key, value in payload.get("deerflow_chat_turns", {}).items()
        }
    )

    CONVERSATIONS.clear()
    CONVERSATIONS.update(
        {key: ConversationDTO.model_validate(value) for key, value in payload.get("conversations", {}).items()}
    )

    MESSAGES.clear()
    MESSAGES.update(
        {
            key: [MessageDTO.model_validate(item) for item in value]
            for key, value in payload.get("messages", {}).items()
        }
    )

    ARTIFACTS.clear()
    ARTIFACTS.update(
        {key: ArtifactCardDTO.model_validate(value) for key, value in payload.get("artifacts", {}).items()}
    )


def _load_state_from_mysql() -> dict | None:
    try:
        from .services.runtime_persistence_service import runtime_persistence_service

        return runtime_persistence_service.get_workflow_state()
    except Exception as exc:  # noqa: BLE001
        print(f"[store] mysql workflow load skipped: {exc}")
        return None


def load_state() -> None:
    payload = None
    if MYSQL_STATE_ENABLED:
        payload = _load_state_from_mysql()
    if not payload and FILE_STATE_ENABLED and STATE_FILE.exists():
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not payload:
        return
    _load_payload(payload)

def clear_runtime_state(*, clear_state_file: bool = True, clear_mysql: bool = True) -> None:
    SCENES.clear()
    SESSIONS.clear()
    QUERY_PLANS.clear()
    QUERY_RUNS.clear()
    REPORT_PLANS.clear()
    SLIDE_DRAFTS.clear()
    DECKS.clear()
    PRESENTATION_TURNS.clear()
    SLIDE_REVISIONS.clear()
    DECK_REVISIONS.clear()
    MEMORY_ENTRIES.clear()
    DEERFLOW_CHAT_TURNS.clear()
    CONVERSATIONS.clear()
    MESSAGES.clear()
    ARTIFACTS.clear()
    PREFERENCES.clear()
    PREFERENCES["default_user"] = PreferenceDTO()
    if clear_state_file and STATE_FILE.exists():
        STATE_FILE.unlink()
    if clear_mysql and MYSQL_STATE_ENABLED:
        try:
            from .services.runtime_persistence_service import runtime_persistence_service

            runtime_persistence_service.clear_runtime_state()
        except Exception as exc:  # noqa: BLE001
            print(f"[store] mysql workflow clear skipped: {exc}")
