"""画布改造分支的守卫单测。

原则（项目硬约束）：**打桩依赖可以，伪造业务数据不行**。
这里只把「会话注册表」当成依赖注入一个空会话，断言的是
「画布上没有大纲时，不许擅自造一个大纲出来」——不构造任何取数结果。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.conversation_service import conversation_service
from app.services.report_composer import report_composer
from app.store import ARTIFACTS, CONVERSATIONS, MESSAGES
from packages.shared_contracts.python_models import ArtifactCardDTO, ConversationDTO, WorkingMemoryDTO


def _now() -> datetime:
    return datetime.now(UTC)


def _empty_conversation(conversation_id: str) -> ConversationDTO:
    now = _now()
    return ConversationDTO(
        conversation_id=conversation_id,
        title="守卫用例",
        scene_id="scene_prd_competitor",
        scene_name="竞品企划分析",
        working_memory=WorkingMemoryDTO(scene_locked_by="explicit"),
        created_at=now,
        updated_at=now,
    )


def _outline_card(conversation_id: str, pages: list[dict]) -> ArtifactCardDTO:
    now = _now()
    return ArtifactCardDTO(
        artifact_id="art_outline1",
        conversation_id=conversation_id,
        kind="outline_card",
        title="ZARA汇报大纲",
        subtitle=f"{len(pages)} 页 · 已绑定 0 页图表",
        status="ready",
        payload={"dedupe_key": "main", "pages": pages, "status": "ready"},
        created_at=now,
        updated_at=now,
    )


def _pages() -> list[dict]:
    return [
        {"order": 1, "title": "封面与核心结论", "objective": "说明范围与口径"},
        {"order": 2, "title": "价格带现状", "objective": "看分布与折扣节奏"},
        {"order": 3, "title": "结论", "objective": "收敛主要发现"},
    ]


def _page(order: int, title: str) -> dict:
    return {"order": order, "title": title, "objective": f"{title}要回答的问题"}


@pytest.fixture()
def conversation_with_outline() -> str:
    conversation_id = "conv_guardtest2"
    conversation = _empty_conversation(conversation_id)
    card = _outline_card(conversation_id, _pages())
    conversation.working_memory.produced_artifact_ids.append(card.artifact_id)
    CONVERSATIONS[conversation_id] = conversation
    MESSAGES[conversation_id] = []
    ARTIFACTS[card.artifact_id] = card
    yield conversation_id

    CONVERSATIONS.pop(conversation_id, None)
    MESSAGES.pop(conversation_id, None)
    for artifact_id in [
        artifact_id
        for artifact_id, item in ARTIFACTS.items()
        if item.conversation_id == conversation_id
    ]:
        ARTIFACTS.pop(artifact_id, None)


@pytest.fixture()
def empty_conversation() -> str:
    """画布完全为空的会话——用来验证「没有产物时不许凭空造」。"""
    conversation_id = "conv_guardtest1"
    CONVERSATIONS[conversation_id] = _empty_conversation(conversation_id)
    MESSAGES[conversation_id] = []
    yield conversation_id

    CONVERSATIONS.pop(conversation_id, None)
    MESSAGES.pop(conversation_id, None)
    for artifact_id in [
        artifact_id
        for artifact_id, item in ARTIFACTS.items()
        if item.conversation_id == conversation_id
    ]:
        ARTIFACTS.pop(artifact_id, None)


def _outline_cards(conversation_id: str) -> list:
    return [
        card
        for card in ARTIFACTS.values()
        if card.conversation_id == conversation_id and card.kind == "outline_card"
    ]


def test_revision_without_outline_never_fabricates_one(empty_conversation: str) -> None:
    """画布上没有大纲时，「改成第 2 页」不能凭空生成一个大纲。"""
    plan = conversation_service._handle_revision(
        empty_conversation, {"raw": "把第2页换掉", "dimension_keys": []}
    )

    assert _outline_cards(empty_conversation) == []
    assert plan["artifact_refs"] == []
    assert plan["actions"] == []
    assert "还没有汇报大纲" in plan["assistant_text"]


def test_revision_without_outline_does_not_run_any_removal(
    empty_conversation: str,
) -> None:
    """说「删掉」也不能触发任何写操作。"""
    plan = conversation_service._handle_revision(
        empty_conversation, {"raw": "删掉色彩那张", "dimension_keys": ["color"]}
    )

    assert plan["artifact_refs"] == []
    assert _outline_cards(empty_conversation) == []


def test_revision_pushes_a_version_so_it_can_be_undone(
    conversation_with_outline: str,
) -> None:
    """改完大纲必须留版本——否则 history 一直是空，用户改错了回不去。"""
    conversation_service._handle_revision(
        conversation_with_outline,
        {"raw": "把第2页换成建议", "page_index": 2, "dimension_keys": []},
    )

    card = _outline_cards(conversation_with_outline)[0]
    assert card.version == 2
    assert len(card.history) == 1
    assert card.history[0]["payload"]["pages"][1]["title"] == "价格带现状"

    restored = conversation_service.rollback_latest(conversation_with_outline)

    assert restored is not None
    assert restored.version == 1
    assert restored.payload["pages"][1]["title"] == "价格带现状"


def test_repeating_the_same_revision_does_not_inflate_versions(
    conversation_with_outline: str,
) -> None:
    """内容没变就不推版本：重复提交不能把版本号刷上去（旧缺陷的防线）。"""
    routing = {"raw": "把第2页换成建议", "page_index": 2, "dimension_keys": []}

    conversation_service._handle_revision(conversation_with_outline, routing)
    conversation_service._handle_revision(conversation_with_outline, routing)

    card = _outline_cards(conversation_with_outline)[0]
    assert card.version == 2, "第二次内容相同，不应再推一个版本"


def test_a_clarifying_turn_never_touches_the_outline(
    conversation_with_outline: str,
) -> None:
    """agent 在反问"哪两页"时，后端不许照着 target.page_index 动手。

    实测事故（2026-10-19）：「把第 9 页和第 10 页合并」agent 回的是
    "只有 4 页…你想合并哪两页？"，但 target.page_index=9 让旧的整页替换
    在末尾 append 了一页「第 9 页补充分析」，还重渲染了 PPT。
    """
    plan = conversation_service._handle_revision(
        conversation_with_outline,
        {
            "raw": "把第9页和第10页合并",
            "needs_clarification": True,
            "clarify_question": "现有大纲是 3 页，你想合并的是哪两页？",
            "page_ops": [{"op": "generate_deck"}],
            "page_index": 9,
        },
    )

    card = _outline_cards(conversation_with_outline)[0]
    assert "哪两页" in plan["assistant_text"]
    assert plan["artifact_refs"] == [] and plan["actions"] == []
    assert card.version == 1 and len(card.payload["pages"]) == 3, "反问轮不许改页、不许造页"
    assert not [c for c in ARTIFACTS.values() if c.kind == "deck_card"], "反问轮不许生成 PPT"


def test_removing_a_dimension_hides_the_chart_but_rollback_brings_it_back(
    conversation_with_outline: str,
) -> None:
    """「去掉色彩」要让图真的从画布消失；回退时还能接回来（卡片本体没删）。"""
    chart = ArtifactCardDTO(
        artifact_id="art_color_guard",
        conversation_id=conversation_with_outline,
        kind="chart_card",
        title="流行色彩矩阵",
        subtitle="色彩",
        status="ready",
        payload={"dimension_key": "color", "spec": {"chart_type": "bar"}, "status": "ready"},
        created_at=_now(),
        updated_at=_now(),
    )
    ARTIFACTS[chart.artifact_id] = chart
    memory = CONVERSATIONS[conversation_with_outline].working_memory
    memory.produced_artifact_ids.append(chart.artifact_id)
    memory.chart_artifact_ids.append(chart.artifact_id)
    # 让大纲里有一页真的绑着这张图，否则"移除维度"没有对应页可删。
    _outline_cards(conversation_with_outline)[0].payload["pages"][2]["bound_artifact_id"] = chart.artifact_id

    conversation_service._handle_revision(
        conversation_with_outline,
        {"raw": "去掉色彩", "remove_dimension_keys": ["color"], "dimension_keys": []},
    )

    assert chart.artifact_id not in memory.produced_artifact_ids, "图必须从画布上消失"
    assert chart.artifact_id in ARTIFACTS, "卡片本体要留着，否则回退接不回来"

    conversation_service.rollback_artifact(_outline_cards(conversation_with_outline)[0].artifact_id)

    assert chart.artifact_id in memory.produced_artifact_ids, "回退后图要回到画布"


def test_replace_page_never_fabricates_a_missing_page() -> None:
    """页码不存在就什么都不做——加页只有 add_page 一条路。"""
    pages = [_page(1, "封面"), _page(2, "价格带"), _page(3, "结论")]

    result = report_composer.replace_page(
        pages=pages, page_index=9, title="第 9 页补充分析", objective="按最新要求补充一页结论。"
    )

    assert len(result) == 3
    assert all(page["order"] <= 3 for page in result)
    assert not any("第 9 页" in str(page.get("title")) for page in result)

