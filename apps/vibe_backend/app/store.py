from __future__ import annotations

import json
import os
from pathlib import Path

from packages.analysis_domain.models import AnalysisSession
from packages.shared_contracts.python_models import (
    AnalysisSessionDTO,
    ArtifactDTO,
    DeckDTO,
    DeerflowChatTurnDTO,
    MemoryEntryDTO,
    PreferenceDTO,
    QueryPlanDTO,
    QueryRunDTO,
    SceneDTO,
    SlideDraftDTO,
)


SCENES: dict[str, SceneDTO] = {}
SESSIONS: dict[str, AnalysisSession] = {}
QUERY_PLANS: dict[str, QueryPlanDTO] = {}
QUERY_RUNS: dict[str, QueryRunDTO] = {}
SLIDE_DRAFTS: dict[str, SlideDraftDTO] = {}
DECKS: dict[str, DeckDTO] = {}
ARTIFACTS: dict[str, ArtifactDTO] = {}
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
        "slide_drafts": _dump_models(SLIDE_DRAFTS),
        "decks": _dump_models(DECKS),
        "artifacts": _dump_models(ARTIFACTS),
        "preferences": _dump_models(PREFERENCES),
        "memory_entries": _dump_models(MEMORY_ENTRIES),
        "deerflow_chat_turns": {key: [item.model_dump(mode="json") for item in value] for key, value in DEERFLOW_CHAT_TURNS.items()},
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
        for session_id, plan_payload in payload.get("query_plans", {}).items():
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
        for artifact_id, artifact_payload in payload.get("artifacts", {}).items():
            session_id = str(artifact_payload.get("session_id") or "").strip()
            if not session_id:
                continue
            runtime_persistence_service.save_workflow_stage(
                session_id=session_id,
                stage_key=f"artifact:{artifact_id}",
                payload=artifact_payload,
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


def _load_payload(payload: dict) -> None:
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

    SLIDE_DRAFTS.clear()
    SLIDE_DRAFTS.update(
        {key: SlideDraftDTO.model_validate(value) for key, value in payload.get("slide_drafts", {}).items()}
    )

    DECKS.clear()
    DECKS.update({key: DeckDTO.model_validate(value) for key, value in payload.get("decks", {}).items()})

    ARTIFACTS.clear()
    ARTIFACTS.update(
        {key: ArtifactDTO.model_validate(value) for key, value in payload.get("artifacts", {}).items()}
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


def _load_state_from_mysql() -> dict | None:
    try:
        from .services.runtime_persistence_service import runtime_persistence_service

        return runtime_persistence_service.get_workflow_state()
    except Exception as exc:  # noqa: BLE001
        print(f"[store] mysql workflow load skipped: {exc}")
        return None


def load_state() -> None:
    payload = None
    if FILE_STATE_ENABLED and STATE_FILE.exists():
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not payload and MYSQL_STATE_ENABLED:
        payload = _load_state_from_mysql()
    if not payload:
        return
    _load_payload(payload)

def clear_runtime_state(*, clear_state_file: bool = True, clear_mysql: bool = True) -> None:
    SCENES.clear()
    SESSIONS.clear()
    QUERY_PLANS.clear()
    QUERY_RUNS.clear()
    SLIDE_DRAFTS.clear()
    DECKS.clear()
    ARTIFACTS.clear()
    MEMORY_ENTRIES.clear()
    DEERFLOW_CHAT_TURNS.clear()
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
