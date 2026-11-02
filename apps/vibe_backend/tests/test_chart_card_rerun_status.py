"""图卡「重跑成功」后，卡片顶层的 status 必须翻回 ready。

报障（2026-10-26）：一张 `status=failed` 的图卡（「SQL Agent 生成了当前数据库
无法命中的标准值过滤」）点重跑，SQL 明明跑出了 62 行，卡片还是顶着「取数失败」——
因为 `revise_artifact` 只写了 `payload.status`，而前端判失败读的是**顶层**
`card.status`（`agent.js`：`card.status === "failed"`）。两份状态各说各话。

本用例只打桩依赖（取数结果、spec 构造），不构造业务数据：
`result_preview` 里那一行只是"驱动返回了一行"的占位。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import app.services.conversation_service as conversation_module
from app.services.conversation_service import conversation_service
from app.store import ARTIFACTS, CONVERSATIONS, MESSAGES
from packages.shared_contracts.python_models import (
    ArtifactCardDTO,
    ConversationDTO,
    SceneDTO,
    WorkingMemoryDTO,
)


class _StubQueryRun:
    status = "succeeded"
    result_preview: list[dict[str, Any]] = [{"sentinel": "stub-row"}]
    sql = "SELECT 1"
    sql_explanation = "stub"
    insight_summary: list[str] = []
    rows_count = 1
    query_id = "query_stub"


def _now() -> datetime:
    return datetime.now(UTC)


def test_rerunning_a_failed_chart_card_flips_the_top_level_status(monkeypatch) -> None:
    conversation_id = "conv_failed_chart_rerun"
    artifact_id = "art_failed_chart_rerun"
    CONVERSATIONS[conversation_id] = ConversationDTO(
        conversation_id=conversation_id,
        title="失败图卡重跑",
        scene_id="scene_test",
        scene_name="测试配置",
        working_memory=WorkingMemoryDTO(scene_locked_by="explicit"),
        created_at=_now(),
        updated_at=_now(),
    )
    MESSAGES[conversation_id] = []
    ARTIFACTS[artifact_id] = ArtifactCardDTO(
        artifact_id=artifact_id,
        conversation_id=conversation_id,
        kind="chart_card",
        title="面料成分与纤维混纺",
        subtitle="",
        status="failed",
        payload={
            "sql": "SELECT 1",
            "status": "failed",
            "error": "SQL Agent 生成了当前数据库无法命中的标准值过滤",
            "spec": {"chart_type": "column"},
        },
        created_at=_now(),
        updated_at=_now(),
    )

    monkeypatch.setattr(
        conversation_service,
        "scene_for_chart",
        lambda conversation_id, card: SceneDTO(
            scene_id="scene_test", name="测试配置", description="结构占位", fields=[]
        ),
    )
    monkeypatch.setattr(conversation_module, "execute_raw_sql", lambda **_: _StubQueryRun())
    monkeypatch.setattr(
        conversation_module.chart_spec_service,
        "build",
        lambda **_: {"chart_type": "column"},
    )
    monkeypatch.setattr(conversation_module.chart_spec_service, "narrative", lambda **_: "")

    try:
        result = conversation_service.execute_chart_sql(
            conversation_id=conversation_id, artifact_id=artifact_id
        )
        card = ARTIFACTS[artifact_id]

        assert result["ok"] is True
        assert card.status == "ready"
        assert card.payload["status"] == "ready"
        assert card.payload["error"] == ""
    finally:
        ARTIFACTS.pop(artifact_id, None)
        CONVERSATIONS.pop(conversation_id, None)
        MESSAGES.pop(conversation_id, None)
